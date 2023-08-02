from math import log
from typing import Optional, Tuple, Union

import torch
import torch.nn.functional as F
from einops import einsum, rearrange
from torch import Tensor, nn

DEFAULT_DEVICE = torch.device("cpu")


def _build_decay_gammas(
    num_heads: int,
    device: torch.device = DEFAULT_DEVICE,
    dtype: torch.dtype = torch.float32,
) -> Tensor:
    """Decay values are different for each retention head, following the prescribed
    method in the paper.  Conceptually, I think of each head having a different
    "retention window", which is the effective number of steps back in time that
    the head can attend to.  Retention windows are effectively determined by
    these decay coefficients.

    See: https://arxiv.org/pdf/2307.08621v3.pdf, Section 3.1 (Setup)
    """
    xmin, xmax = log(1 / 32), log(1 / 512)
    x = torch.linspace(xmin, xmax, steps=num_heads, device=device, dtype=dtype)
    return 1 - torch.exp(x)


def _build_decay_mask(
    query_length: int,
    key_length: int,
    decay_gammas: Tensor,
    device: torch.device = DEFAULT_DEVICE,
    dtype: torch.dtype = torch.float32,
) -> Tensor:
    """The decay mask is one of the key components that makes *parallel* retention
    equivalent to *recursive* retention.  The decay coefficients are pre-computed
    and applied to the similarity matrix at once, rather than being applied to
    each element in the recursive formulation.

    See: https://arxiv.org/pdf/2307.08621v3.pdf, Equation 5
    """
    query_pos = torch.arange(query_length, device=device, dtype=dtype)
    key_pos = torch.arange(key_length, device=device, dtype=dtype)

    distance = torch.abs(query_pos.unsqueeze(-1) - key_pos.unsqueeze(0))
    # Set the upper-triangular distances to infinity, so that only *past* keys
    # can affect the current query.  (Setting distance to infinity ensures that
    # the decay matrix is 0 for those positions, since x^(inf) = 0 when -1 < x < 1.
    distance_mask = torch.ones_like(distance, dtype=torch.bool).triu_(diagonal=1)
    distance = distance.masked_fill(distance_mask, float("inf"))

    distance = rearrange(distance, "n s -> () n s")
    decay_gammas = rearrange(decay_gammas, "h -> h () ()")
    return decay_gammas**distance


def retention_parallel(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    decay_gammas: Optional[Tensor] = None,
    need_weights: bool = False,
) -> Tuple[Tensor, Optional[Tensor]]:
    if decay_gammas is None:
        decay_gammas = _build_decay_gammas(
            num_heads=query.shape[1],
            device=query.device,
            dtype=query.dtype,
        )
    decay_mask = _build_decay_mask(
        query_length=query.shape[2],
        key_length=key.shape[2],
        decay_gammas=decay_gammas,
        device=query.device,
        dtype=query.dtype,
    )

    # einstein notation:
    # - b: batch_size
    # - h: num_heads
    # - n / s: seq_length
    # - d: hidden_dim
    similarity = einsum(query, key, "b h n d, b h s d -> b h n s")
    similarity = similarity * rearrange(decay_mask, "h n s -> () h n s")
    retention = einsum(similarity, value, "b h n s, b h s d -> b h n d")

    if need_weights:
        return retention, similarity
    else:
        return retention, None


def retention_recurrent(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    prev_state: Optional[Tensor],
    decay_gammas: Optional[Tensor] = None,
) -> Tuple[Tensor, Tensor]:
    if decay_gammas is None:
        decay_gammas = _build_decay_gammas(
            num_heads=query.shape[1],
            device=query.device,
            dtype=query.dtype,
        )

    # einstein notation:
    # - b: batch_size
    # - h: num_heads
    # - d: hidden_dim
    state = einsum(key, value, "b h d, b h m -> b h d m")
    if prev_state is not None:
        state = state + prev_state * rearrange(decay_gammas, "h -> () h () ()")
    retention = einsum(query, state, "b h d, b h d m -> b h m")

    # if group_norm is not None:
    #     retention = group_norm(retention)

    return retention, state


class MultiheadRetention(nn.Module):
    """Multi-head retention (MHR) layer.  Intended to be (mostly) a drop-in replacement
    for nn.MultiheadAttention, but with the option to use either the parallel or
    recursive formulation of retention. (Attention only has the parallel formulation.)

    Reference:
        "Retentive Network: A Successor to Transformer for Large Language Models"
        https://arxiv.org/pdf/2307.08621v3.pdf
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        dropout: float = 0.0,
        bias: bool = True,
        batch_first: bool = True,
        group_norm_eps: float = 1e-6,
        device: Optional[Union[torch.device, str]] = None,
        dtype: Optional[torch.dtype] = None,
        # TODO???
        # add_bias_kv=False,
        # add_zero_attn=False,
        # kdim=None,
        # vdim=None,
    ):
        if not batch_first:
            raise NotImplementedError("batch_first=False is not yet supported")

        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.bias = bias
        self.batch_first = batch_first

        if embed_dim % self.num_heads != 0:
            raise ValueError(
                f"embed_dim ({embed_dim}) must be divisible by num_heads ({num_heads})"
            )

        head_dim = embed_dim // num_heads
        if not head_dim % 8 == 0:
            raise ValueError(
                f"head_dim (embed_dim / num_heads = {head_dim}) must be divisible by 8"
            )
        if not head_dim <= 128:
            raise ValueError(
                f"head_dim (embed_dim / num_heads = {head_dim}) must be <= 128"
            )

        # The q/k/v projection layers are the same as in vanilla MHA.
        self.q_proj = nn.Linear(
            embed_dim, embed_dim, bias=bias, device=device, dtype=dtype
        )
        self.k_proj = nn.Linear(
            embed_dim, embed_dim, bias=bias, device=device, dtype=dtype
        )
        self.v_proj = nn.Linear(
            embed_dim, embed_dim, bias=bias, device=device, dtype=dtype
        )
        self.group_norm = nn.GroupNorm(
            num_groups=num_heads,
            num_channels=num_heads,
            affine=False,
            eps=group_norm_eps,
            device=device,
            dtype=dtype,
        )
        # The output project is slightly different, due to the "swish" gated layer.
        self.g_proj = nn.Linear(
            embed_dim, embed_dim, bias=bias, device=device, dtype=dtype
        )
        self.out_proj = nn.Linear(
            embed_dim, embed_dim, bias=bias, device=device, dtype=dtype
        )

        self._reset_parameters()

    def _reset_parameters(self):
        nn.init.xavier_normal_(self.q_proj.weight)
        if self.q_proj.bias is not None:
            nn.init.constant_(self.q_proj.bias, 0)
        nn.init.xavier_normal_(self.k_proj.weight)
        if self.k_proj.bias is not None:
            nn.init.constant_(self.k_proj.bias, 0)

        # TODO: Double-check that we're following the same initialization as in
        # the paper.  This is a generic initialization for MHA linear layers.
        nn.init.xavier_normal_(self.v_proj.weight)
        if self.v_proj.bias is not None:
            nn.init.constant_(self.v_proj.bias, 0)
        nn.init.xavier_normal_(self.out_proj.weight)
        if self.out_proj.bias is not None:
            nn.init.constant_(self.out_proj.bias, 0)
        nn.init.xavier_normal_(self.g_proj.weight)
        if self.g_proj.bias is not None:
            nn.init.constant_(self.g_proj.bias, 0)

    def forward_parallel(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        need_weights: bool = False,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        # einstein notation:
        # b - batch size
        # n - sequence length
        # h - number of heads
        # d - embedding dimension
        #
        # Input shape: (b, n, d)
        q: Tensor = self.q_proj(query)
        k: Tensor = self.k_proj(key)
        v: Tensor = self.v_proj(value)

        # Unfold 'd' dimension into 'h' separate retention heads.  Move the head
        # dimension to position 1 (makes matrix ops *much* faster).
        q = rearrange(q, "b n (h d) -> b h n d", h=self.num_heads)
        k = rearrange(k, "b n (h d) -> b h n d", h=self.num_heads)
        v = rearrange(v, "b n (h d) -> b h n d", h=self.num_heads)
        # Apply retention, then fold 'h' retention heads back into 'd'.
        retention, weights = retention_parallel(q, k, v, need_weights=need_weights)

        # To apply group norm in an equivalent way to the recursive formulation,
        # we fold the sequence dimension into the batch dimension.  Otherwise,
        # normalization would be applied over the entire input sequence.
        batch_size = retention.size(0)
        retention = rearrange(retention, "b h n d -> (b n) h d")
        retention = F.dropout(retention, p=self.dropout, training=self.training)
        retention = self.group_norm(retention)
        # Unfold 'n' from the batch dimension, and fold 'h' back into the embed dim.
        retention = rearrange(retention, "(b n) h d -> b n (h d)", b=batch_size)

        # NOTE: Unlike multihead attention, the retention paper applies a "swish"
        # gate to increase the non-linear capacity of the model.  (IMO this is likely
        # to make up for the lack of "softmax" activation in the retention mechanism.)
        #
        # The paper describes the gate as:
        #   g = swish(X * W_g)
        # where X is the input to the layer.  The authors only use Retention in a
        # Decoder-style module, the q/k/v inputs are the same (i.e. X = q = k = v).
        # So, I assume that 'query' can equivalently be used as the input.
        gate = torch.nn.functional.silu(self.g_proj(query))
        retention = self.out_proj(retention * gate)

        return retention, weights

    def forward_recurrent(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        prev_state: Optional[Tensor],
    ) -> Tuple[Tensor, Optional[Tensor]]:
        # einstein notation:
        # b - batch size
        # h - number of heads
        # d - embedding dimension
        #
        # input shape: (b, d)
        q: Tensor = self.q_proj(query)
        k: Tensor = self.k_proj(key)
        v: Tensor = self.v_proj(value)

        # Unfold 'd' dimension into 'h' separate retention heads.
        q = rearrange(q, "b (h d) -> b h d", h=self.num_heads)
        k = rearrange(k, "b (h d) -> b h d", h=self.num_heads)
        v = rearrange(v, "b (h d) -> b h d", h=self.num_heads)
        # Apply retention then group norm.
        retention, state = retention_recurrent(q, k, v, prev_state=prev_state)
        retention = F.dropout(retention, p=self.dropout, training=self.training)
        retention = self.group_norm(retention)
        # Fold heads back into the embedding dimension.
        retention = rearrange(retention, "b h d -> b (h d)")

        # NOTE: Unlike multihead attention, the retention paper applies a "swish"
        # gate to increase the non-linear capacity of the model.  (IMO this is likely
        # to make up for the lack of "softmax" activation in the retention mechanism.)
        #
        # The paper describes the gate as:
        #   g = swish(X * W_g)
        # where X is the input to the layer.  The authors only use Retention in a
        # Decoder-style module, the q/k/v inputs are the same (i.e. X = q = k = v).
        # So, I assume that 'query' can equivalently be used as the input.
        gate = torch.nn.functional.silu(self.g_proj(query))
        retention = self.out_proj(retention * gate)

        return retention, state

    # TODO
    # def forward_chunkwise

    def forward(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        need_weights: bool = False,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        return self.forward_parallel(query, key, value, need_weights=need_weights)


if __name__ == "__main__":
    batch_size = 1
    seq_len = 8
    embed_dim = 16
    num_heads = 2
    device = "cuda"
    dtype = torch.float32

    query = torch.randn(batch_size, seq_len, embed_dim, device=device, dtype=dtype)
    key = torch.randn(batch_size, seq_len, embed_dim, device=device, dtype=dtype)
    value = torch.randn(batch_size, seq_len, embed_dim, device=device, dtype=dtype)
    mhr = MultiheadRetention(
        embed_dim, num_heads, batch_first=True, device=device, dtype=dtype
    ).eval()

    with torch.no_grad():
        yp, weights = mhr.forward_parallel(query, key, value)
        print(yp[:, 2])

        prev_state: Optional[Tensor] = None
        for i in range(3):
            q, k, v = query[:, i], key[:, i], value[:, i]
            yr, prev_state = mhr.forward_recurrent(q, k, v, prev_state)
            print(yr)
