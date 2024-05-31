"""
Based on:
Chen, L., Ye, Z., Wu, Y., Zhuo, D., Ceze, L., & Krishnamurthy, A. (2023). 
Punica: Multi-Tenant LoRA Serving. 
https://arxiv.org/abs/2310.18547
"""

import triton
import triton.language as tl
import torch


@triton.jit
def _bgmv_shrink_kernel(
    input_ptr,
    lora_ptr,
    out_ptr,
    N,
    K,
    lora_indices,
    scaling,
    xm_stride,
    xk_stride,
    l0_stride,
    lora_k_stride,
    lora_n_stride,
    cm_stride,
    cn_stride,
    BLOCK_N: tl.constexpr,
    BLOCK_K: tl.constexpr,
):
    cur_batch = tl.program_id(axis=0)
    lora_index = tl.load(lora_indices + cur_batch)
    if lora_index == -1:
        return

    offset_n = tl.arange(0, BLOCK_N)
    offset_k = tl.arange(0, BLOCK_K)
    a_ptr = input_ptr + cur_batch * xm_stride
    b_ptr = lora_ptr + l0_stride * lora_index
    rank_mask = offset_n[:, None] < N
    accumulator = tl.zeros((BLOCK_N,), dtype=tl.float32)
    for k in range(0, K, BLOCK_K):
        current_k = k + offset_k
        # vector load
        current_k_c = tl.max_contiguous(current_k, BLOCK_K)
        tiled_a = tl.load(
            a_ptr + current_k_c * xk_stride,
            mask=current_k < K,
            other=0.0,
        )  # [BLOCK_K]
        b_ptr_mask = (rank_mask < N) & (current_k[None, :] < K)

        tiled_b = tl.load(
            b_ptr
            + offset_n[:, None] * lora_k_stride
            + current_k[None, :] * lora_n_stride,
            mask=b_ptr_mask,
            other=0.0,
        )  # [BLOCK_N,BLOCK_K]

        accumulator += tl.sum(tiled_a * tiled_b, 1)
    accumulator *= scaling
    offset_cn = tl.arange(0, BLOCK_N)
    c_ptr = out_ptr + cur_batch * cm_stride + offset_cn * cn_stride
    c_mask = offset_cn < N

    tl.store(c_ptr, accumulator, mask=c_mask)


@torch.inference_mode()
def bgmv_shrink(
    inputs: torch.Tensor,
    lora_a_weights: torch.Tensor,
    output_tensor: torch.Tensor,
    lora_indices_tensor: torch.Tensor,
    batchs: int,
    scaling: float,
):
    """

    Args:
        inputs (torch.Tensor): input tensor
        lora_a_weights (torch.Tensor): lora'a weight
        output_tensor (torch.Tensor): output tensor
        lora_indices_tensor (torch.Tensor): (batch_size,). The LoRA index
            corresponding to each batch
        batchs (int): batch size
        scaling (float):  Scaling factor.
    """
    assert inputs.dtype == lora_a_weights.dtype
    assert inputs.dtype in [torch.float16, torch.bfloat16, torch.float32]
    assert inputs.size(1) == lora_a_weights.size(-1)
    assert lora_indices_tensor.size(0) == batchs
    assert inputs.is_contiguous()

    if lora_a_weights.ndim == 4:  # shape:(lora_num,1,rank, size)
        assert lora_a_weights.size(1) == 1
        lora_a_weights = lora_a_weights.squeeze(dim=1)
    else:
        assert lora_a_weights.ndim == 3  # shape:(lora_num,rank, size)
    assert lora_a_weights.is_contiguous()
    assert output_tensor.is_contiguous()
    # TODO tuning this config
    N, K = lora_a_weights.shape[-2:]  # K=hidden_size,N=rank
    BLOCK_K = 512
    BLOCK_N = triton.next_power_of_2(output_tensor.size(1))
    grid = [
        batchs,
    ]
    config = {"num_stages": 4, "num_warps": 8}
    _bgmv_shrink_kernel[grid](
        inputs,
        lora_a_weights,
        output_tensor,
        N,
        K,
        lora_indices_tensor,
        scaling,
        inputs.stride(0),
        inputs.stride(1),
        lora_a_weights.stride(0),
        lora_a_weights.stride(1),
        lora_a_weights.stride(2),
        output_tensor.stride(0),
        output_tensor.stride(1),
        BLOCK_N,
        BLOCK_K,
        **config,
    )
    return
