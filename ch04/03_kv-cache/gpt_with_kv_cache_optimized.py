# This file collects all the relevant code that we covered thus far
# throughout Chapters 3-4.
# This file can be run as a standalone script.

import time
import tiktoken
import torch
import torch.nn as nn


#####################################
# Chapter 3
#####################################
class MultiHeadAttention(nn.Module):
    def __init__(self, d_in, d_out, context_length, dropout, num_heads, qkv_bias=False, max_seq_len=None, window_size=None):
        super().__init__()
        assert d_out % num_heads == 0, "d_out must be divisible by num_heads"

        self.d_out = d_out
        self.num_heads = num_heads
        self.head_dim = d_out // num_heads  # Reduce the projection dim to match desired output dim

        self.W_query = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.W_key = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.W_value = nn.Linear(d_in, d_out, bias=qkv_bias)
        self.out_proj = nn.Linear(d_out, d_out)  # Linear layer to combine head outputs
        self.dropout = nn.Dropout(dropout)

        ####################################################
        # NEW
        self.max_seq_len = max_seq_len or context_length
        self.window_size = window_size or self.max_seq_len
        self.register_buffer("cache_k", None, persistent=False)
        self.register_buffer("cache_v", None, persistent=False)
        ####################################################

    def forward(self, x, use_cache=False):
        b, num_tokens, d_in = x.shape

        keys_new = self.W_key(x)  # Shape: (b, num_tokens, d_out)
        values_new = self.W_value(x)
        queries = self.W_query(x)

        # We implicitly split the matrix by adding a `num_heads` dimension
        # Unroll last dim: (b, num_tokens, d_out) -> (b, num_tokens, num_heads, head_dim)
        keys_new = keys_new.view(b, num_tokens, self.num_heads, self.head_dim)
        values_new = values_new.view(b, num_tokens, self.num_heads, self.head_dim)
        queries = queries.view(b, num_tokens, self.num_heads, self.head_dim)

        # Transpose: (b, num_tokens, num_heads, head_dim) -> (b, num_heads, num_tokens, head_dim)
        keys_new = keys_new.transpose(1, 2)
        values_new = values_new.transpose(1, 2)
        queries = queries.transpose(1, 2)

        ####################################################
        # NEW
        if use_cache:
            if self.cache_k is None or self.cache_k.size(0) != b:
                self.cache_k = torch.zeros(b, self.num_heads,
                                           self.window_size, self.head_dim,
                                           device=x.device)
                self.cache_v = torch.zeros_like(self.cache_k)
                self.ptr_cur = 0  # pointer to next free slot

            # if incoming chunk would overflow discard oldest tokens
            if self.ptr_cur + num_tokens > self.window_size:
                overflow = self.ptr_cur + num_tokens - self.window_size
                # shift everything left by `overflow` (cheap view-copy)
                self.cache_k[:, :, :-overflow, :] = self.cache_k[:, :, overflow:, :].clone()
                self.cache_v[:, :, :-overflow, :] = self.cache_v[:, :, overflow:, :].clone()
                self.ptr_cur -= overflow  # pointer after shift

            self.cache_k[:, :, self.ptr_cur:self.ptr_cur + num_tokens, :] = keys_new
            self.cache_v[:, :, self.ptr_cur:self.ptr_cur + num_tokens, :] = values_new
            self.ptr_cur += num_tokens

            keys = self.cache_k[:, :, :self.ptr_cur, :]
            values = self.cache_v[:, :, :self.ptr_cur, :]
        else:
            keys, values = keys_new, values_new
            self.ptr_cur = 0  # keep pointer sane if you interleave modes
        ####################################################
        # Compute scaled dot-product attention (aka self-attention) with a causal mask
        attn_scores = queries @ keys.transpose(2, 3)  # Dot product for each head

        ####################################################
        # NEW
        K = attn_scores.size(-1)

        if num_tokens == K:
            # No cache → use the pre‑baked triangular mask slice
            causal_mask = torch.triu(torch.ones(num_tokens, K, device=x.device, dtype=torch.bool), diagonal=1)
        else:
            # Cached: need to offset the diagonal by (K − num_tokens)
            offset = K - num_tokens  # number of tokens already in cache before this chunk
            row_idx = torch.arange(num_tokens, device=x.device).unsqueeze(1)  # (num_tokens, 1)
            col_idx = torch.arange(K, device=x.device).unsqueeze(0)           # (1, K)
            causal_mask = row_idx + offset < col_idx                          # True where j > i+offset
        ####################################################

        # Use the mask to fill attention scores
        attn_scores.masked_fill_(causal_mask.unsqueeze(0).unsqueeze(0), -torch.inf)

        attn_weights = torch.softmax(attn_scores / keys.shape[-1]**0.5, dim=-1)
        attn_weights = self.dropout(attn_weights)

        # Shape: (b, num_tokens, num_heads, head_dim)
        context_vec = (attn_weights @ values).transpose(1, 2)

        # Combine heads, where self.d_out = self.num_heads * self.head_dim
        context_vec = context_vec.contiguous().view(b, num_tokens, self.d_out)
        context_vec = self.out_proj(context_vec)  # optional projection

        return context_vec

    ####################################################
    # NEW
    def reset_cache(self):
        self.cache_k, self.cache_v = None, None
    ####################################################


#####################################
# Chapter 4
#####################################
class LayerNorm(nn.Module):
    def __init__(self, emb_dim):
        super().__init__()
        self.eps = 1e-5
        self.scale = nn.Parameter(torch.ones(emb_dim))
        self.shift = nn.Parameter(torch.zeros(emb_dim))

    def forward(self, x):
        mean = x.mean(dim=-1, keepdim=True)
        var = x.var(dim=-1, keepdim=True, unbiased=False)
        norm_x = (x - mean) / torch.sqrt(var + self.eps)
        return self.scale * norm_x + self.shift


class GELU(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return 0.5 * x * (1 + torch.tanh(
            torch.sqrt(torch.tensor(2.0 / torch.pi)) *
            (x + 0.044715 * torch.pow(x, 3))
        ))


class FeedForward(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(cfg["emb_dim"], 4 * cfg["emb_dim"]),
            GELU(),
            nn.Linear(4 * cfg["emb_dim"], cfg["emb_dim"]),
        )

    def forward(self, x):
        return self.layers(x)


class TransformerBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.att = MultiHeadAttention(
            d_in=cfg["emb_dim"],
            d_out=cfg["emb_dim"],
            context_length=cfg["context_length"],
            num_heads=cfg["n_heads"],
            dropout=cfg["drop_rate"],
            qkv_bias=cfg["qkv_bias"],
            window_size=cfg["kv_window_size"])  # NEW
        self.ff = FeedForward(cfg)
        self.norm1 = LayerNorm(cfg["emb_dim"])
        self.norm2 = LayerNorm(cfg["emb_dim"])
        self.drop_shortcut = nn.Dropout(cfg["drop_rate"])

    def forward(self, x, use_cache=False):
        # Shortcut connection for attention block
        shortcut = x
        x = self.norm1(x)

        # x = self.att(x)   # Shape [batch_size, num_tokens, emb_size]
        ####################################################
        # NEW
        x = self.att(x, use_cache=use_cache)
        ####################################################

        x = self.drop_shortcut(x)
        x = x + shortcut  # Add the original input back

        # Shortcut connection for feed-forward block
        shortcut = x
        x = self.norm2(x)
        x = self.ff(x)
        x = self.drop_shortcut(x)
        x = x + shortcut  # Add the original input back

        return x


class GPTModel(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"])
        self.pos_emb = nn.Embedding(cfg["context_length"], cfg["emb_dim"])
        self.drop_emb = nn.Dropout(cfg["drop_rate"])

        # self.trf_blocks = nn.Sequential(
        #    *[TransformerBlock(cfg) for _ in range(cfg["n_layers"])])
        ####################################################
        # NEW
        self.trf_blocks = nn.ModuleList(
            [TransformerBlock(cfg) for _ in range(cfg["n_layers"])])

        self.ptr_current_pos = 0
        ####################################################

        self.final_norm = LayerNorm(cfg["emb_dim"])
        self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=False)

    def forward(self, in_idx, use_cache=False):
        batch_size, seq_len = in_idx.shape
        tok_embeds = self.tok_emb(in_idx)

        # pos_embeds = self.pos_emb(torch.arange(seq_len, device=in_idx.device))

        ####################################################
        # NEW

        if use_cache:
            pos_ids = torch.arange(self.ptr_current_pos, self.ptr_current_pos + seq_len, device=in_idx.device, dtype=torch.long)
            self.ptr_current_pos += seq_len
        else:
            pos_ids = torch.arange(0, seq_len, device=in_idx.device, dtype=torch.long)
        pos_embeds = self.pos_emb(pos_ids).unsqueeze(0)
        ####################################################

        x = tok_embeds + pos_embeds  # Shape [batch_size, num_tokens, emb_size]
        x = self.drop_emb(x)

        # x = self.trf_blocks(x)
        ####################################################
        # NEW
        for blk in self.trf_blocks:
            x = blk(x, use_cache=use_cache)
        ####################################################

        x = self.final_norm(x)
        logits = self.out_head(x)
        return logits

    ####################################################
    # NEW
    def reset_kv_cache(self):
        for blk in self.trf_blocks:
            blk.att.reset_cache()
        self.ptr_current_pos = 0
    ####################################################


def generate_text_simple(model, idx, max_new_tokens, context_size):
    # idx is (B, T) array of indices in the current context
    for _ in range(max_new_tokens):

        # Crop current context if it exceeds the supported context size
        # E.g., if LLM supports only 5 tokens, and the context size is 10
        # then only the last 5 tokens are used as context
        idx_cond = idx[:, -context_size:]

        # Get the predictions
        with torch.no_grad():
            logits = model(idx_cond)

        # Focus only on the last time step
        # (batch, n_token, vocab_size) becomes (batch, vocab_size)
        logits = logits[:, -1, :]

        # Get the idx of the vocab entry with the highest logits value
        idx_next = torch.argmax(logits, dim=-1, keepdim=True)  # (batch, 1)

        # Append sampled index to the running sequence
        idx = torch.cat((idx, idx_next), dim=1)  # (batch, n_tokens+1)

    return idx


####################################################
# NEW
def generate_text_simple_cached(model, idx, max_new_tokens, use_cache=True):
    model.eval()

    ctx_len = model.pos_emb.num_embeddings  # max supported length, e.g. 1024
    if use_cache:
        # Init cache with full prompt
        model.reset_kv_cache()
        with torch.no_grad():
            logits = model(idx[:, -ctx_len:], use_cache=True)

        for _ in range(max_new_tokens):
            # a) pick the token with the highest log-probability (greedy sampling)
            next_idx = logits[:, -1].argmax(dim=-1, keepdim=True)
            # b) append it to the running sequence
            idx = torch.cat([idx, next_idx], dim=1)
            # c) feed model only the new token
            with torch.no_grad():
                logits = model(next_idx, use_cache=True)
    else:
        for _ in range(max_new_tokens):
            with torch.no_grad():
                logits = model(idx[:, -ctx_len:], use_cache=False)
            next_idx = logits[:, -1].argmax(dim=-1, keepdim=True)
            idx = torch.cat([idx, next_idx], dim=1)

    return idx
####################################################


def main():
    GPT_CONFIG_124M = {
        "vocab_size": 50257,     # Vocabulary size
        "context_length": 1024,  # Context length
        "emb_dim": 768,          # Embedding dimension
        "n_heads": 12,           # Number of attention heads
        "n_layers": 12,          # Number of layers
        "drop_rate": 0.1,        # Dropout rate
        "qkv_bias": False,       # Query-Key-Value bias
        "kv_window_size": 1024   # NEW: KV cache window size
    }

    torch.manual_seed(123)
    model = GPTModel(GPT_CONFIG_124M)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()  # disable dropout

    start_context = "Hello, I am"

    tokenizer = tiktoken.get_encoding("gpt2")
    encoded = tokenizer.encode(start_context)
    encoded_tensor = torch.tensor(encoded, device=device).unsqueeze(0)

    print(f"\n{50*'='}\n{22*' '}IN\n{50*'='}")
    print("\nInput text:", start_context)
    print("Encoded input text:", encoded)
    print("encoded_tensor.shape:", encoded_tensor.shape)

    if torch.cuda.is_available():
        torch.cuda.synchronize()
    start = time.time()

    # token_ids = generate_text_simple(
    #     model=model,
    #     idx=encoded_tensor,
    #     max_new_tokens=200,
    #     context_size=GPT_CONFIG_124M["context_length"]
    # )

    ####################################################
    # NEW
    token_ids = generate_text_simple_cached(
        model=model,
        idx=encoded_tensor,
        max_new_tokens=200,
    )
    ####################################################

    if torch.cuda.is_available():
        torch.cuda.synchronize()
    total_time = time.time() - start

    decoded_text = tokenizer.decode(token_ids.squeeze(0).tolist())

    print(f"\n\n{50*'='}\n{22*' '}OUT\n{50*'='}")
    print("\nOutput:", token_ids)
    print("Output length:", len(token_ids[0]))
    print("Output text:", decoded_text)

    print(f"\nTime: {total_time:.2f} sec")
    print(f"{int(len(token_ids[0])/total_time)} tokens/sec")
    if torch.cuda.is_available():
        max_mem_bytes = torch.cuda.max_memory_allocated()
        max_mem_gb = max_mem_bytes / (1024 ** 3)
        print(f"Max memory allocated: {max_mem_gb:.2f} GB")


if __name__ == "__main__":
    main()
