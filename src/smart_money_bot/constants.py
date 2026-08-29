"""Chain constants used by the monitor and executor."""

BOT_VERSION = "2.37.0"
PAPER_DEMO_MINT = "PAPER-DEMO-ONLY"
PAPER_DEMO_ENTRY_PRICE_USD = "1"

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

STABLE_MINTS = {USDC_MINT, USDT_MINT}
QUOTE_MINTS = STABLE_MINTS | {WRAPPED_SOL_MINT}

LIVE_ACK_TEXT = "I_UNDERSTAND_LIVE_TRADING_CAN_LOSE_ALL_FUNDS"
PUMP_LAUNCH_ACK_TEXT = "I_UNDERSTAND_PUMP_LAUNCHES_SPEND_REAL_SOL"

# Addresses that must never form a wallet "cluster".  These are protocol-level
# accounts, not counterparties: two wallets that both touched the system program
# share nothing.  Centralised-exchange hot wallets create the same false
# clustering and cannot be enumerated reliably here, so add them per deployment
# through FOMO_RUNNER_EXCLUDED_FUNDERS.
INFRASTRUCTURE_ADDRESSES = frozenset(
    {
        "11111111111111111111111111111111",  # System program
        "ComputeBudget111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # SPL Token-2022
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated token account
        "1nc1nerator11111111111111111111111111111111",  # Incinerator
        "SysvarRent111111111111111111111111111111111",
        WRAPPED_SOL_MINT,
    }
)
