# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0
import pytest
import torch
from loguru import logger

import ttnn
from models.demos.wormhole.mistral7b.reference.model import Attention, precompute_freqs_cis
from models.demos.wormhole.mistral7b.tt.mistral_attention import TtMistralAttention
from models.demos.wormhole.mistral7b.tt.mistral_common import (
    get_prefill_rot_mat,
    get_rot_transformation_mat,
    prepare_inputs_ttnn_prefill,
)
from models.demos.wormhole.mistral7b.tt.model_config import TtModelArgs
from models.utility_functions import comp_allclose, comp_pcc, skip_for_grayskull


@skip_for_grayskull("Requires wormhole_b0 to run")
@pytest.mark.parametrize(
    "seq_len",
    (
        4096,
        128,
    ),
)
def test_mistral_attention_inference(seq_len, device, use_program_cache, reset_seeds):
    dtype = ttnn.bfloat8_b
    pcc = 0.99

    model_args = TtModelArgs(device)
    state_dict = torch.load(model_args.consolidated_weights_path)

    # Ref model needs partial state dict, but our models use full state dict keys as cached weight names
    partial_state_dict = {k[19:]: v for k, v in state_dict.items() if (k.startswith("layers.0.attention."))}
    reference_model = Attention(args=model_args)
    reference_model.load_state_dict(partial_state_dict)

    batch = 1

    # pre-compute the rotational embedding matrix and send to device
    rot_mats = get_prefill_rot_mat(model_args.head_dim, model_args.max_seq_len, device, seq_len=seq_len)
    transformation_mat_torch = get_rot_transformation_mat(model_args.head_dim)
    transformation_mats = ttnn.as_tensor(
        transformation_mat_torch,
        dtype=ttnn.bfloat16,
        layout=ttnn.TILE_LAYOUT,
        device=device,
        memory_config=ttnn.DRAM_MEMORY_CONFIG,
    )
    generation_start_pos = 0
    generation_length = 3
    all_tests_pass = True

    tt_model = TtMistralAttention(
        [device],
        state_dict,
        weight_cache_path=model_args.weight_cache_path(dtype),
        layer_num=0,
        dtype=dtype,
        configuration=model_args,
        rot_mat=None,
        start_pos=generation_start_pos,
    )

    for i in range(generation_length):
        pt_attention_input = (torch.rand(batch, seq_len, model_args.dim) * 2) - 1
        tt_attention_input = pt_attention_input.clone()
        attention_input, attn_mask, attn_mask_torch = prepare_inputs_ttnn_prefill(
            tt_attention_input,
            device,
        )

        tt_out = tt_model([attention_input], 0, [attn_mask], rot_mats, transformation_mats, user_id=0, mode="prefill")
        # multi-device attention module returns replicated output
        assert isinstance(tt_out, list)
        tt_out = tt_out[0]
        tt_output_torch = ttnn.to_torch(tt_out).view(batch, seq_len, -1)  # [ batch, seq, hidden_dim]

        positions = torch.LongTensor(range(seq_len))
        freqs_cis_i = precompute_freqs_cis(model_args.head_dim, 128_000)[positions]
        reference_output = reference_model(pt_attention_input, freqs_cis_i, positions, attn_mask_torch)

        passing, pcc_message = comp_pcc(reference_output, tt_output_torch, pcc)

        logger.info(comp_allclose(reference_output, tt_output_torch))
        logger.info(pcc_message)

        if passing:
            logger.info(f"Mistral_Attention Passed!")
        else:
            logger.warning(f"Mistral_Attention Failed!")
            all_tests_pass = False

        if False:  # FIXME: Issue #10648
            # Check kv cache
            # PyTorch output --------------------------------------------------------------------
            pytorch_layer_present = [
                reference_model.cache_k.clone().permute(0, 2, 1, 3),  # [batch, n_kv_heads, seq, head_dim]
                reference_model.cache_v.clone().permute(0, 2, 1, 3),  # [batch, n_kv_heads, seq, head_dim]
            ]
            # TT hardware execution -------------------------------------------------------------
            tt_layer_present = []
            for layer_past in tt_model.layer_past_list:
                tt_layer_present.append([ttnn.to_torch(cache) for cache in layer_past])

            tt_layer_present = tt_layer_present[0]

            for i, (cache_pt, cache_tt) in enumerate(zip(pytorch_layer_present, tt_layer_present)):
                cache_length_to_check = min(model_args.sliding_window, generation_start_pos + generation_length + 1)
                cache_pt = cache_pt[:, :, generation_start_pos:cache_length_to_check, :]
                cache_tt = cache_tt[:, :, generation_start_pos:cache_length_to_check, :]
                does_pass, output_pcc = comp_pcc(cache_pt, cache_tt, pcc)
                if i == 0:
                    logger.info(f"K cache output: {output_pcc}")
                else:
                    logger.info(f"V cache output: {output_pcc}")

                if does_pass:
                    logger.info(f"KV Cache Passed!")
                else:
                    logger.warning(f"KV Cache Failed! PCC value is lower than {pcc}")
                    all_tests_pass = False

    if all_tests_pass:
        logger.info("Mistral Attention output Passed!")
    else:
        logger.warning("Mistral Attention output Failed!")
        assert all_tests_pass, f"PCC value is lower than {pcc} for some of the outputs. Check Warnings!"
