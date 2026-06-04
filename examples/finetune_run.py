# -----------------------------------------------------------------------------
# Setup: HF_TOKEN is required
# -----------------------------------------------------------------------------
# This script downloads google/gemma-3-1b-it, which is a gated model. You need
# a Hugging Face access token. Get one at https://huggingface.co/settings/tokens
# and accept the model license at https://huggingface.co/google/gemma-3-1b-it
#
# 1. Set HF_TOKEN in your local shell, persistently:
#
#       echo 'export HF_TOKEN=hf_yourTokenHere' | tee -a ~/.zshrc ~/.bashrc
#       source ~/.zshrc                    # or open a new terminal
#       echo $HF_TOKEN                     # verify — should print your token
#
# 2. Pipe the local env var into the colab kernel before running this script:
#
#       echo "import os; os.environ['HF_TOKEN'] = '$HF_TOKEN'" | colab exec
#
# 3. Verify the kernel received it:
#
#       echo 'import os; print(bool(os.environ.get("HF_TOKEN")))' | colab exec
#       # → should print: True
#
# 4. Run this script:
#
#       colab exec -f finetune_run.py
#
# Note: HF_TOKEN lives in the colab kernel for the lifetime of the session.
# If you `colab stop` or the session expires, you'll need to re-pipe it (step 2).
# -----------------------------------------------------------------------------

import os

os.system("pip install -q -U 'bitsandbytes>=0.46.1'")

import torch
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model
from trl import SFTConfig, SFTTrainer

MODEL_ID = "google/gemma-3-1b-it"
NUM_SAMPLES = 200  # demo size; bump to 5000+ for a real run
MAX_STEPS = 60  # demo cap; set to -1 for full-epoch training

# -------- Data --------
# philschmid/gretel-synthetic-text-to-sql has sql_prompt, sql_context, sql.
# We hand SFTTrainer a "messages" column and let it apply the chat template.
print("Loading dataset...")
dataset = load_dataset("philschmid/gretel-synthetic-text-to-sql", split="train").select(
    range(NUM_SAMPLES)
)


def to_messages(example):
    user_msg = (
        "You are a SQL expert. Given the schema, write a SQL query that "
        "answers the question. Reply with only the SQL.\n\n"
        f"Schema:\n{example['sql_context']}\n\n"
        f"Question:\n{example['sql_prompt']}"
    )
    return {
        "messages": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": example["sql"]},
        ]
    }


dataset = dataset.map(to_messages, remove_columns=dataset.column_names)

# -------- Model (4-bit QLoRA, bf16 throughout) --------
# Everything is bf16 — matches Gemma's natural dtype, matches TRL's default
# T4 (Turing) has no hardware bf16, so this is slower than fp16 would be (~2x)
print(f"Loading {MODEL_ID} in 4-bit...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    quantization_config=BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    ),
    device_map="auto",
)

model = get_peft_model(
    model,
    LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules="all-linear",
        task_type="CAUSAL_LM",
    ),
)
# Required for QLoRA backward: makes the embedding output require grad so that
# gradients can flow into the LoRA params attached to layers downstream of the
# frozen 4-bit base.
model.enable_input_require_grads()
model.print_trainable_parameters()

# -------- Train --------
# All other knobs use SFTConfig defaults (which include bf16=True,
# gradient_checkpointing=True, logging_steps=10). The overrides below are just
# the demo cap, batch sizing that fits T4 VRAM, and silencing wandb/tensorboard.
print("Training...")
trainer = SFTTrainer(
    model=model,
    train_dataset=dataset,
    processing_class=tokenizer,
    args=SFTConfig(
        output_dir="./results",
        max_steps=MAX_STEPS,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=2,
        # Standard QLoRA LR. SFTConfig defaults to 2e-5, which is too low for
        # LoRA adapters to learn anything meaningful in 60 steps.
        learning_rate=2e-4,
        # Compute loss only on the assistant's SQL, not on the schema/question.
        assistant_only_loss=True,
        # Off so KV cache works during the inference step at the end.
        gradient_checkpointing=False,
        report_to="none",
    ),
)
trainer.train()

# -------- Save --------
out_dir = "./gemma-3-1b-qlora-adapter"
trainer.model.save_pretrained(out_dir)
tokenizer.save_pretrained(out_dir)
print(f"Saved adapter to {out_dir}")

# -------- Inference check --------
sample = dataset[0]
prompt = tokenizer.apply_chat_template(
    sample["messages"][:1],  # just the user turn
    tokenize=False,
    add_generation_prompt=True,
)
inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
with torch.no_grad():
    out_ids = model.generate(
        **inputs,
        max_new_tokens=256,
        do_sample=False,
        pad_token_id=tokenizer.pad_token_id,
    )
generated = tokenizer.decode(
    out_ids[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True
)
print(f"\nGold:  {sample['messages'][1]['content']}")
print(f"Model: {generated.strip()}")
