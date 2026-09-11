"""Black-Scholes-Merton pricing, implied-vol inversion and probability helpers.

Pure-python (math.erf) so the bot has no scipy dependency. Everything here is
expressed in *per-share* option terms; multiply by 100 for contract dollars.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT_2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0

# Below this vega (price change per 1 vol point, per share) an option price
# carries no usable information about implied volatility.
MIN_VEGA_FOR_IV = 1e-4


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    vol_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_t
    return d1, d1 - vol_t


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             kind: str = "put", q: float = 0.0) -> float:
    """European option price. `kind` is 'put' or 'call'."""
    kind = kind.lower()
    if T <= 0:
        return max(0.0, (K - S) if kind == "put" else (S - K))
    if sigma <= 0:
        fwd = S * math.exp(-q * T) - K * math.exp(-r * T)
        return max(0.0, -fwd if kind == "put" else fwd)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if kind == "call":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float          # per calendar day, per share
    vega: float           # per 1 vol point (0.01), per share
    iv: float


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float,
              kind: str = "put", q: float = 0.0) -> Greeks:
    kind = kind.lower()
    if T <= 0 or sigma <= 0:
        intrinsic_delta = (-1.0 if S < K else 0.0) if kind == "put" else (1.0 if S > K else 0.0)
        return Greeks(intrinsic_delta, 0.0, 0.0, 0.0, max(sigma, 0.0))

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_q, disc_r = math.exp(-q * T), math.exp(-r * T)
    pdf_d1 = norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * disc_q * pdf_d1 * math.sqrt(T) * 0.01

    common_theta = -(S * disc_q * pdf_d1 * sigma) / (2.0 * math.sqrt(T))
    if kind == "call":
        delta = disc_q * norm_cdf(d1)
        theta = common_theta - r * K * disc_r * norm_cdf(d2) + q * S * disc_q * norm_cdf(d1)
    else:
        delta = -disc_q * norm_cdf(-d1)
        theta = common_theta + r * K * disc_r * norm_cdf(-d2) - q * S * disc_q * norm_cdf(-d1)

    return Greeks(delta, gamma, theta / DAYS_PER_YEAR, vega, sigma)


def implied_vol(price: float, S: float, K: float, T: float, r: float,
                kind: str = "put", q: float = 0.0,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> float | None:
    """Invert BS for sigma by bisection. Returns None when the price is
    outside the no-arbitrage band (stale/crossed quotes, deep ITM parity)."""
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    intrinsic = max(0.0, (K * math.exp(-r * T) - S * math.exp(-q * T)) if kind == "put"
                    else (S * math.exp(-q * T) - K * math.exp(-r * T)))
    if price < intrinsic - 1e-6:
        return None
    if price > (K if kind == "put" else S) + 1e-6:
        return None

    p_lo = bs_price(S, K, T, r, lo, kind, q)
    p_hi = bs_price(S, K, T, r, hi, kind, q)
    if price <= p_lo:
        return lo
    if price >= p_hi:
        return hi

    sigma = None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        p_mid = bs_price(S, K, T, r, mid, kind, q)
        if abs(p_mid - price) < tol:
            sigma = mid
            break
        if p_mid < price:
            lo = mid
        else:
            hi = mid
    if sigma is None:
        sigma = 0.5 * (lo + hi)

    # Identifiability guard. For a deep-OTM, short-dated option the price is
    # ~1e-10 and vega is ~0: every sigma over a wide range reproduces the price
    # to within tolerance, so the "implied vol" that comes back is an artefact
    # of where bisection happened to stop. Returning None is the honest answer;
    # callers already treat a missing IV as "cannot evaluate this contract".
    vega = bs_greeks(S, K, T, r, sigma, kind, q).vega
    if vega < MIN_VEGA_FOR_IV:
        return None
    return sigma


def prob_itm(S: float, K: float, T: float, r: float, sigma: float,
             kind: str = "put", q: float = 0.0) -> float:
    """Risk-neutral P(finish ITM) = N(-d2) for puts, N(d2) for calls."""
    if T <= 0:
        return 1.0 if ((S < K) if kind == "put" else (S > K)) else 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if d2 is None:
        return 0.0
    return norm_cdf(-d2) if kind == "put" else norm_cdf(d2)


def prob_touch(S: float, K: float, T: float, sigma: float, r: float = 0.0,
               q: float = 0.0) -> float:
    """P(barrier K is touched at any time before T) under GBM (first-passage).

    Roughly 2x prob_itm for short-dated OTM strikes -- this, not prob_itm, is
    the number that governs how often a stop-loss gets tagged.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    mu = r - q - 0.5 * sigma * sigma
    vol_t = sigma * math.sqrt(T)
    b = math.log(K / S)
    nu = mu / (sigma * sigma)

    z1 = (b - mu * T) / vol_t
    z2 = (b + mu * T) / vol_t
    if b < 0:                                   # down-and-in barrier
        p = norm_cdf(z1) + math.exp(2.0 * nu * b) * norm_cdf(z2)
    else:                                       # up-and-in barrier
        p = (1.0 - norm_cdf(z1)) + math.exp(2.0 * nu * b) * (1.0 - norm_cdf(z2))
    return min(1.0, max(0.0, p))


def expected_move(S: float, sigma: float, days: float) -> float:
    """1-sigma move over `days` calendar days, in price terms."""
    return S * sigma * math.sqrt(max(days, 0.0) / DAYS_PER_YEAR)
