import os
import pdb
import copy
import math
import numpy as np 
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import torch
from torch import nn
import torch.utils.checkpoint
import torch.nn.functional as F
from torch.cuda.amp import autocast
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss


from transformers.models.llama.configuration_llama import LlamaConfig
from transformers.models.llama.modeling_llama import LlamaRotaryEmbedding, LlamaAttention, apply_rotary_pos_emb


__all__ = ['convert_kvcache_llama_heavy_recent', 'LlamaAttention_heavy_hitter']


def low_dimension_attention(query_states, key_states, heavy_budget, recent_budget, penalty, penalty_mode):

    cache_budget = heavy_budget + recent_budget

    # attn_weights (BS, head, query, keys)
    dtype_query_states = query_states.dtype
    
    batch_size = query_states.shape[0]
    head_num = query_states.shape[1]
    seq_length = query_states.shape[2]
    state_dimension = query_states.shape[3]
    
    history_mask = torch.zeros(batch_size, head_num, seq_length, dtype=dtype_query_states, device=query_states.device)
    small_dimensions = None
    
    attn_shape = (batch_size, head_num, seq_length, seq_length)
    result_attention = torch.zeros(attn_shape, dtype=dtype_query_states, device=query_states.device)

    for token_index in range(seq_length):
        if token_index > cache_budget:
            if small_dimensions is None:
                _, small_dimensions = keys[:,:,:token_index-1,:].abs().mean(dim=-2).topk(state_dimension-6, largest=False, dim=-1)
            
            history = history_mask[:,:,:token_index] + tmp_attn.squeeze(2)
            
            if recent_budget != 0:
                _, unnecessary_tokens = history[:,:,:-recent_budget].topk(1, largest=False, dim=-1)
            else:
                _, unnecessary_tokens = history[:,:,:].topk(1, largest=False, dim=-1)
            
            batch_indices, head_indices = torch.meshgrid(torch.arange(batch_size), torch.arange(head_num))
            batch_indices_exp = batch_indices.unsqueeze(-1).expand_as(unnecessary_tokens)
            head_indices_exp = head_indices.unsqueeze(-1).expand_as(unnecessary_tokens)
            
            keys[batch_indices_exp, head_indices_exp, unnecessary_tokens, small_dimensions] = 0
            history_mask[batch_indices_exp, head_indices_exp, unnecessary_tokens] = torch.inf
            
        query = query_states[:,:,token_index,:].unsqueeze(2)
        keys = key_states[:,:,:token_index+1,:]
        
        tmp_attn = torch.matmul(query, keys.transpose(2,3))/math.sqrt(state_dimension)
        result_attention[:,:,token_index,:token_index+1] = tmp_attn.squeeze(2)
            
    return result_attention

def local_heavy_hitter_mask(attn_weights, heavy_budget, recent_budget, penalty, penalty_mode):

    # attn_weights (BS, head, query, keys)
    dtype_attn_weights = attn_weights.dtype
    seq_length = attn_weights.shape[-1]
    
    cache_budget = heavy_budget + recent_budget
    score_shape = attn_weights[:,:,0,:].shape

    select_score = torch.zeros(score_shape, dtype=torch.float, device=attn_weights.device)
    penalty_score = torch.zeros(score_shape, dtype=torch.float, device=attn_weights.device)
    penalty_divider = torch.zeros(score_shape, dtype=torch.float, device=attn_weights.device)
    
    mask_bottom = torch.zeros_like(attn_weights, dtype=torch.bool)
    mask_bottom[:,:,0,0] = True # First Token

    score_cache_index = 0
    score_cache_budget = 3
    score_cache = torch.zeros_like(attn_weights[:,:,:score_cache_budget,:], dtype=torch.float, device=attn_weights.device)

    for token_index in range(seq_length-1):
        # Current Step Calculate
        current_mask = mask_bottom[:,:,token_index,:]
        
        tmp_attn = current_mask * attn_weights[:,:,token_index,:] + ~current_mask*torch.finfo(attn_weights.dtype).min
        tmp_attn = torch.softmax(tmp_attn, dim=-1, dtype=torch.float32).to(dtype_attn_weights)
        
        if penalty_mode: # Fixed Penalty
            select_score = penalty*select_score + tmp_attn
        else: # Token-wise Penalty
            score_cache[:,:,score_cache_index,:] = tmp_attn
            score_cache_index = (score_cache_index + 1) % score_cache_budget
            
            select_score = torch.mean(score_cache, dim=-2)
        
        select_score *= current_mask
        
        # Next Mask Make
        local_index = token_index - recent_budget
        if token_index >= cache_budget:
            if heavy_budget > 0:
                _, tmp_topk_index = torch.topk(select_score[:,:,:local_index+1], k=heavy_budget, dim=-1)
                mask_bottom[:,:,token_index+1,:] = mask_bottom[:,:,token_index+1,:].scatter(-1, tmp_topk_index, True) # (head, keys)
            
            mask_bottom[:,:,token_index+1, local_index+1:token_index+2] = True # recent
        else:
            mask_bottom[:,:,token_index+1, :token_index+2] = True # recent
    
    return mask_bottom

class LlamaAttention_heavy_hitter(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(self, config: LlamaConfig):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.max_position_embeddings = config.max_position_embeddings

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )
        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=False)
        self.k_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=False)
        self.rotary_emb = LlamaRotaryEmbedding(self.head_dim, max_position_embeddings=self.max_position_embeddings)

        self.heavy_budget_ratio = config.heavy_ratio
        self.recent_budget_ratio = config.recent_ratio
        self.penalty = config.penalty
        self.penalty_mode = config.penalty_mode

    def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        
        ### Heavy + Recent
        heavy_budget = int(self.heavy_budget_ratio * hidden_states.shape[-2])
        recent_budget = int(self.recent_budget_ratio * hidden_states.shape[-2])

        bsz, q_len, _ = hidden_states.size()
            
        query_states = self.q_proj(hidden_states).view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = self.k_proj(hidden_states).view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        value_states = self.v_proj(hidden_states).view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)
        # [bsz, nh, t, hd]

        if past_key_value is not None:
            # reuse k, v, self_attention
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)

        past_key_value = (key_states, value_states) if use_cache else None

        ################################################################################################
        
        # attn_weights = torch.matmul(query_states, key_states.transpose(2, 3)) / math.sqrt(self.head_dim)

        attn_weights = low_dimension_attention(
            query_states=query_states,
            key_states=key_states,
            heavy_budget=heavy_budget,
            recent_budget=recent_budget,
            penalty=self.penalty,
            penalty_mode=self.penalty_mode
        )
        
        ################################################################################################

        if attn_weights.size() != (bsz, self.num_heads, q_len, kv_seq_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz, self.num_heads, q_len, kv_seq_len)}, but is"
                f" {attn_weights.size()}"
            )

        if attention_mask is not None:
            if attention_mask.size() != (bsz, 1, q_len, kv_seq_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)}, but is {attention_mask.size()}"
                )
            attn_weights = attn_weights + attention_mask
            attn_weights = torch.max(attn_weights, torch.tensor(torch.finfo(attn_weights.dtype).min))
        
        ################################################################################################
        
        # mask_bottom = local_heavy_hitter_mask(
        #     attn_weights=attn_weights,
        #     heavy_budget=heavy_budget,
        #     recent_budget=recent_budget,
        #     penalty=self.penalty,
        #     penalty_mode=self.penalty_mode
        # ) # Default: No padding applied to input
        
        # attn_weights[~mask_bottom] = torch.min(attention_mask)
        
        ################################################################################################

        # upcast attention to fp32
        attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
        attn_output = torch.matmul(attn_weights, value_states)

        if attn_output.size() != (bsz, self.num_heads, q_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
                f" {attn_output.size()}"
            )

        attn_output = attn_output.transpose(1, 2)
        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)
        
        attn_output = self.o_proj(attn_output)

        if not output_attentions:
            attn_weights = None
        
        return attn_output, attn_weights, past_key_value


def convert_kvcache_llama_heavy_recent(model, config):

    for name, module in reversed(model._modules.items()):

        if len(list(module.children())) > 0:
            model._modules[name] = convert_kvcache_llama_heavy_recent(module, config)

        if isinstance(module, LlamaAttention) or isinstance(module, LlamaAttention_heavy_hitter):
            model._modules[name] = LlamaAttention_heavy_hitter(config)

    return model