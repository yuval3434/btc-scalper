# Polymarket V2 Cutover Runbook (COMPLETED)

## Migration Status
**Status:** Complete  
**Completion Date:** April 28, 2026

## Post-Migration Notes & Anomalies (Lessons Learned)
*   **Order Wiping:** All open orders were successfully wiped during the transition as expected.
*   **Order Replacement:** The bot's opportunistic nature meant no complex standing orders needed manual script recreation. Any orphaned positions were safely managed via the UI.
*   **WebSocket Resiliency:** The V2 WebSocket stream occasionally includes new fields (e.g., `timestamp`, `metadata`, `builder`). The bot's dynamic dictionary parsing (`.get()`) handled these gracefully without throwing deserialization errors.
*   **Balance Parameter Requirement:** For `FAK` Market Buys, the V2 API strictly enforces passing `userUSDCBalance` (now representing pUSD) to accurately calculate fee-adjusted fills.

## 4. Standing Orders / Market-Making Note
Because this bot operates as an opportunistic scalper (re-evaluating entry conditions tick-by-tick), it does not maintain long-term standing liquidity. However, if the bot was manually killed while holding a position, you will need to manually re-place any Take-Profit/Stop-Loss orders via the Polymarket UI once the API is back online.

## 5. Rollback Notes
**There is no V1 fallback.** After the cutover, the V1 exchange contracts and matching engine will be fully deprecated. 
* If the V2 API misbehaves or order placements fail, **the only option is to halt the bot.** 
* Wait for upstream fixes from the Polymarket team before resuming live trading. Do not attempt to revert SDK versions or contract addresses.