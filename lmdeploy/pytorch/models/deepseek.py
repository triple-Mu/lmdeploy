# Copyright (c) OpenMMLab. All rights reserved.
from typing import Optional, Tuple

import torch
import torch.distributed as dist
from torch import nn

from lmdeploy.pytorch.kernels.fused_moe import fused_moe

from ..kernels import apply_rotary_pos_emb, fill_kv_cache, paged_attention_fwd
from ..weight_loader.dist_utils import (colwise_parallelize_linear,
                                        rowwise_parallelize_linear)


class PatchedDeepseekAttention(nn.Module):

    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""
        for mod_name in ['q_proj', 'k_proj', 'v_proj']:
            colwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)
        for mod_name in ['o_proj']:
            rowwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)

    @classmethod
    def _distribute_output_fn(cls, outputs, **kwargs):
        """Distribution output hook."""
        dist.all_reduce(outputs[0])
        return outputs

    def _contiguous_batching_forward_impl(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        world_size: int = 1,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor],
               Optional[Tuple[torch.Tensor]]]:
        """Rewrite implementation of forward.

        Add continuous batching support. Add paged attention support. TP
        support.
        """
        context = self.context.context
        kv_seq_length = context.kv_seq_length
        q_seq_length = context.q_seq_length
        q_start_loc = context.q_start_loc
        block_offsets = context.block_offsets
        max_q_seq_length = context.max_q_seq_length
        max_kv_seq_length = context.max_kv_seq_length

        num_heads = self.num_heads // world_size
        num_kv_heads = self.num_key_value_heads // world_size
        head_dim = self.head_dim
        hidden_size = num_heads * head_dim

        def __qkv_proj(hidden_states):
            """qkv proj."""
            query_states = self.q_proj(hidden_states)
            key_states = self.k_proj(hidden_states)
            value_states = self.v_proj(hidden_states)

            return query_states, key_states, value_states

        def __rotary_emb_fn(query_states, key_states, value_states):
            if hasattr(self, 'rotary_emb'):
                if not hasattr(context, '_cos'):
                    cos, sin = self.rotary_emb(value_states,
                                               seq_len=max_kv_seq_length)
                    context._cos = cos
                    context._sin = sin
                else:
                    cos = context._cos
                    sin = context._sin
                query_states, key_states = apply_rotary_pos_emb(
                    query_states,
                    key_states,
                    cos,
                    sin,
                    position_ids,
                    context.position_ids_1d,
                    q_embed=query_states,
                    k_embed=key_states)
            return query_states, key_states, value_states

        query_states, key_states, value_states = __qkv_proj(hidden_states)

        query_states = query_states.view(-1, num_heads, head_dim)
        key_states = key_states.view(-1, num_kv_heads, head_dim)
        value_states = value_states.view(-1, num_kv_heads, head_dim)

        query_states, key_states, value_states = __rotary_emb_fn(
            query_states, key_states, value_states)

        fill_kv_cache(
            key_states,
            value_states,
            past_key_value[0],
            past_key_value[1],
            q_start_loc,
            q_seq_length,
            kv_seq_length=kv_seq_length,
            max_q_seq_length=max_q_seq_length,
            block_offsets=block_offsets,
        )

        attn_output = query_states
        paged_attention_fwd(
            query_states,
            past_key_value[0],
            past_key_value[1],
            attn_output,
            block_offsets,
            q_start_loc=q_start_loc,
            q_seqlens=q_seq_length,
            kv_seqlens=kv_seq_length,
            max_seqlen=max_q_seq_length,
        )
        attn_output = attn_output.reshape(*hidden_states.shape[:-1],
                                          hidden_size)

        attn_output = self.o_proj(attn_output)

        return attn_output, None, past_key_value

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        **kwargs
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor],
               Optional[Tuple[torch.Tensor]]]:
        """forward."""
        world_size = 1
        if dist.is_initialized():
            world_size = dist.get_world_size()
        return self._contiguous_batching_forward_impl(
            hidden_states,
            position_ids,
            past_key_value,
            output_attentions,
            world_size=world_size,
        )


def _div_up(a, b):
    """div up."""
    return (a + b - 1) // b


class PatchedDeepseekMoE(nn.Module):

    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""

        def __load_mlp(exp_id, exp):
            """load mlp."""
            with loader.prefix_context(f'experts.{exp_id}'):
                loader.load_model_weights(
                    exp,
                    rank=rank,
                    world_size=world_size,
                    device=device,
                    load_only=True,
                )

        def __drop_mlp(exp_id, exp):
            """drop mlp."""
            for name, _ in exp.named_parameters(recurse=True):
                loader.pop(f'experts.{exp_id}.{name}')

        num_experts = len(self.experts)
        exp_per_rank = _div_up(num_experts, world_size)
        first_exp = rank * exp_per_rank
        last_exp = min(num_experts, first_exp + exp_per_rank)
        for exp_id, exp in enumerate(self.experts):
            if first_exp <= exp_id < last_exp:
                __load_mlp(exp_id, exp)
            else:
                __drop_mlp(exp_id, exp)
        self.experts = self.experts[first_exp:last_exp]
        with loader.prefix_context('gate'):
            loader.load_model_weights(self.gate,
                                      rank=rank,
                                      world_size=world_size,
                                      device=device)

        if self.config.n_shared_experts is not None:
            with loader.prefix_context('shared_experts'):
                loader.load_model_weights(self.shared_experts,
                                          rank=rank,
                                          world_size=world_size,
                                          device=device)

    def _update_model_fn(self):
        """update model."""
        num_experts = len(self.experts)

        def __get_meta():
            exp = self.experts[0]
            ffn_dim = exp.gate_proj.weight.size(0)
            hidden_dim = exp.down_proj.weight.size(0)
            dtype = exp.gate_proj.weight.dtype
            device = exp.gate_proj.weight.device
            return ffn_dim, hidden_dim, dtype, device

        def __copy_assign_param(param, weight):
            """copy assign."""
            weight.copy_(param.data)
            param.data = weight

        ffn_dim, hidden_dim, dtype, device = __get_meta()

        gate_up_weights = torch.empty(num_experts,
                                      ffn_dim * 2,
                                      hidden_dim,
                                      device=device,
                                      dtype=dtype)
        down_weights = torch.empty(num_experts,
                                   hidden_dim,
                                   ffn_dim,
                                   device=device,
                                   dtype=dtype)

        for exp_id, exp in enumerate(self.experts):
            __copy_assign_param(exp.gate_proj.weight,
                                gate_up_weights[exp_id, :ffn_dim])
            __copy_assign_param(exp.up_proj.weight, gate_up_weights[exp_id,
                                                                    ffn_dim:])
            __copy_assign_param(exp.down_proj.weight, down_weights[exp_id])

        torch.cuda.empty_cache()

        self.register_buffer('gate_up_weights', gate_up_weights)
        self.register_buffer('down_weights', down_weights)

    def forward(self, hidden_states):
        """forward."""
        world_size = 1
        rank = 0
        if dist.is_initialized():
            world_size = dist.get_world_size()
            rank = dist.get_rank()
        exp_per_rank = self.gate_up_weights.size(0)
        expert_offset = rank * exp_per_rank

        identity = hidden_states
        orig_shape = hidden_states.shape
        topk_idx, topk_weight, _ = self.gate(hidden_states)
        hidden_states = hidden_states.flatten(0, 1)
        flat_topk_idx = topk_idx.flatten()
        y = fused_moe(hidden_states,
                      self.gate_up_weights,
                      self.down_weights,
                      topk_weight,
                      flat_topk_idx,
                      topk=self.num_experts_per_tok,
                      expert_offset=expert_offset,
                      num_experts=world_size * exp_per_rank,
                      renormalize=False).view(*orig_shape)
        if self.config.n_shared_experts is not None:
            y = y + self.shared_experts.forward(identity)
        if dist.is_initialized():
            dist.all_reduce(y)
        return y
