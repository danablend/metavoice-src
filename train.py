import os
from pathlib import Path

import torch
import tqdm
from encodec import EncodecModel
from torch.cuda.amp import GradScaler, autocast
from torch.nn import functional as F
from torch.optim import SGD

from dataloader import create_metavoice_dataloader
from fam.llm.fast_inference_utils import (
    build_model,
    logits_to_probs
)
from fam.llm.adapters import FlattenedInterleavedEncodec2Codebook
from fam.llm.utils import get_default_dtype, get_device
from lora import TransformerWithLoRA, freeze_parameters_except_lora

from accelerate import Accelerator


# Launch this (train.py) with:
# FOR bfloat16:     "accelerate launch train.py --mixed_precision bf16"
# FOR float16:      "accelerate launch train.py --mixed_precision f16"

accelerator = Accelerator()

class MetaVoiceTrainer:
    ENCODEC_BANDWIDTH = 6.0
    END_OF_AUDIO_TOKEN = 1024

    def __init__(self, model_dir: str = 'models', dataset_dir: str = 'dataset', train_stage_two: bool = False, seed: int = 1337):
        self._model_dir = model_dir
        self._dataset_dir = dataset_dir
        self._output_dir = f"{self._model_dir}/outputs"

        os.makedirs(self._output_dir, exist_ok=True)

        # NOTE: this needs to come first so that we don't change global state when we want to use
        # the torch.compiled-model.
        self._dtype = get_default_dtype()
        # self._device = get_device()
        self._device = str(accelerator.device)
        self._model_dir = 'models'
        # self._model_dir = snapshot_download(repo_id=model_name)

        self.encodec_model = EncodecModel.encodec_model_24khz().to(self._device)
        self.encodec_model.set_target_bandwidth(self.ENCODEC_BANDWIDTH)

        self.first_stage_adapter = FlattenedInterleavedEncodec2Codebook(end_of_audio_token=self.END_OF_AUDIO_TOKEN)

        print("building model")
        self.precision = {"float16": torch.float16, "bfloat16": torch.bfloat16}[self._dtype]
        self.model, self.tokenizer, self.smodel, self.model_size = build_model(
            precision=self.precision,
            checkpoint_path=Path(f"{self._model_dir}/first_stage.pt"),
            spk_emb_ckpt_path=Path(f"{self._model_dir}/speaker_encoder.pt"),
            device=self._device,
            compile=False,
            compile_prefill=True,
        )

        # Add LoRA to stage 1 model for finetuning
        self.model = TransformerWithLoRA.from_base_model(self.model, precision=self.precision, device=self._device)

    def train(self, training_name: str, epochs=10, learning_rate=1e-4):
        print("Initializing dataloader...")

        # Model configuration for training
        top_p = 0.95
        guidance_scale = 3.0
        temperature = 1.0

        temperature = torch.tensor(temperature, device=self._device, dtype=self.precision)
        top_k = None
        top_p = torch.tensor(top_p, device=self._device, dtype=self.precision)
        guidance_scale = torch.tensor(guidance_scale, device=self._device, dtype=self.precision)

        dataloader = create_metavoice_dataloader(
            dataset_dir=self._dataset_dir,
            encodec_model=self.encodec_model,
            tokenizer=self.tokenizer, 
            spkemb_model=self.smodel, 
            batch_size=2, 
            device=self._device
        )
        # TODO(validation data) load validation data as well for val loss

        # Freeze stage 1 model parameters except LoRA layers
        print("Freezing model parameters (except LoRA layers)...")
        freeze_parameters_except_lora(self.model)


        print("Initializing optimizer and scaler...")
        optimizer = SGD(self.model.parameters(), lr=learning_rate)
        scaler = GradScaler()

        # Prepare model, optimizer, and dataloader for mixed precision training
        print("Preparing model, optimizer, and dataloader for mixed precision training...")
        self.model, optimizer, dataloader = accelerator.prepare(self.model, optimizer, dataloader)
        
        # Put model in training mode
        print("Setting model to training mode...")
        self.model.train()

        # Ensure the directory for saving models exists
        print("Preparing model folder...")
        save_dir = os.path.join('saved_models', training_name)
        os.makedirs(save_dir, exist_ok=True)

        print("Starting training...")
        for epoch in range(epochs):
            total_loss = 0.0
            for batch_idx, (prompts, encodec_tokens, speaker_embeddings) in enumerate(dataloader):
                print("Batch ", (batch_idx + 1))

                # Move data to device & cast to appropriate dtype
                # prompts = prompts.to(self._device, dtype=torch.long)
                # encodec_tokens = encodec_tokens.to(self._device, dtype=torch.long)
                # speaker_embeddings = speaker_embeddings.to(self._device, dtype=self.precision)

                # B = batch size
                # P = length of prompt
                # T = length of encodec tokens
                # V = vocab size (2562 for metavoice-1b)

                # prompts -> (B, P)
                # encodec_tokens -> (B, T)
                print("Prompts shape: ", prompts.shape)
                print("Encodec tokens shape: ", encodec_tokens.shape)

                # 1. Determine random encodec token location
                # [0, T_rand-1]
                T_rand = torch.randint(0, encodec_tokens.shape[1], (1,)).item()

                # 2. Compute input_pos
                # input_pos -> (P + T_rand)
                input_pos = torch.arange(0, prompts.shape[1] + T_rand, device=self._device, dtype=torch.int)

                # 3. Arrange input tokens
                # input_tokens -> (B, P + T_rand)
                input_tokens = torch.cat([prompts, encodec_tokens[:, :T_rand]], dim=1)

                # 4. Get GT output token labels
                # gt_output_tokens -> (B, 1)
                gt_output_tokens = encodec_tokens[:, T_rand]

                optimizer.zero_grad()
                with autocast(dtype=self.precision):
                    # 5. Compute logits
                    # logits -> (B, P + T_rand, V)

                    # Print dtypes of all model inputs
                    # print("input_tokens dtype: ", input_tokens.dtype)
                    # print("speaker_embeddings dtype: ", speaker_embeddings.dtype)
                    # print("input_pos dtype: ", input_pos.dtype)
                    logits: torch.Tensor = self.model(input_tokens, speaker_embeddings, input_pos)

                    # 6. Rearrange logits 
                    # (B, P + T_rand, V) -> (B, V, P + T_rand)
                    logits = logits.permute(0, 2, 1)
                    
                    # 7. Get the newly generated token logits (the last token)
                    # (B, V, P + T_rand) -> (B, V)
                    logits = logits[:, :, -1]
                    
                    # 8. Compute loss -> (1,)
                    print("Logits shape: ", logits.shape)
                    print("GT output tokens shape: ", gt_output_tokens.shape)
                    loss = F.cross_entropy(logits, gt_output_tokens)

                # 9. Backward pass
                accelerator.backward(loss)

                # 10. Step optimizer
                optimizer.step()
                
                # Print batch loss
                print(f'Batch {batch_idx+1}, Loss: {loss.item():.4f}')
            
            # TODO(validation loss) implement validation loss

            avg_loss = total_loss / len(dataloader)
            print(f'Epoch {epoch+1}, Avg Loss: {avg_loss:.4f}')
            
            # Save model after each epoch
            epoch_save_path = os.path.join(save_dir, f'{training_name}_epoch_{epoch+1}.pt')
            torch.save(self.model.state_dict(), epoch_save_path)
            print(f'Model saved to {epoch_save_path}')

        print("Training complete")

if __name__ == '__main__':
    model_dir = 'models'

    # Expected data directory format:
    # <dataset_dir>/<UTT_1>.wav <dataset_dir>/<UTT_1>.txt
    # <dataset_dir>/<UTT_2>.wav <dataset_dir>/<UTT_2>.txt
    # <dataset_dir>/<UTT_3>.wav <dataset_dir>/<UTT_3>.txt
    # txt files should contain the transcript of the audio
    # etc...
    dataset_dir = 'dummy_dataset'
    training_name = 'finetune_001'


    print("Initializing trainer...")
    trainer = MetaVoiceTrainer(
        model_dir=model_dir,
        dataset_dir=dataset_dir
    )

    print("Training...")
    trainer.train(
        training_name
    )