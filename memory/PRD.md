# PotionRH
Original request: pull the repo and run it; later an isometric voxel island game. June 2026: add RainbowKit wallet login (Robinhood Chain mainnet only, mobile wallet deep-link), game starts with no characters, Market + Inventory with 6 character levels ($1/$10/$50/$100/$200/$400), card art based on the in-game characters, purchases saved in MongoDB. Then: when the island is full, offer a $2 island expansion.

User choices: purchases are DEMO (no payment taken), bought characters are placed on the island and work, same level can be bought many times, inventory is tied to the wallet and stored on the server.

## Architecture
- `/` -> `pages/GamePage.jsx`: iframe `public/game.html` (Three.js r160) + React overlay (main menu, bottom bar, Market/Inventory sheets). `/admin` admin panel.
- Wallet: `lib/wallet.js` (RainbowKit + wagmi, single chain id 4663), `hooks/useSession.js` (SIWE message via `viem/siwe`, JWT kept in localStorage per address, 7 days).
- React -> iframe via postMessage: `potion:state {placed[{id,level,slot}], potion, rate, plots, focus, focusPlot}`, `potion:focus {id}`; iframe -> React `potion:ready`.
- Backend `server.py`: `/api/auth/nonce`, `/api/auth/verify` (python `siwe`, chain 4663 only), `/api/game/catalog|state|buy|place|recall|expand`. Collections: `nonces` (TTL, marked used), `game_players` (potion, potion_at), `game_items` (one doc per purchase, placed/slot), `game_expansions`.
- Rules: LEVELS (price, per_min, max_placed 16/12/6/4/8/4), expansion $2, max 4, extras per expansion 4/4/2/1/2/1. Slot -> position mapping lives in game.html (`SLOTS`, `slotPos`, plots are 40x16 land strips on the south edge).

## Implemented
- Voxel island, potion tank, mines, Robinhood castle (earlier work). Ambient NPCs/machines removed: island starts empty.
- 2026-06: wallet login + chain lock, market, inventory, place/recall/find, server-side potion accrual, six unit models (miner, pump worker, small drill, big drill tower, hose extractor, factory), AI card art in `public/assets/cards`, island expansion plots. Backend 22/22 tests pass (`backend/tests/test_wallet_game.py`); UI checked by screenshots with an injected test provider.

- 2026-06: leaderboard. `GET /api/game/leaderboard` (player JWT) ranks wallets by characters owned, ties by island expansions; top 20 + caller's own row, addresses shortened server-side. UI: `LeaderboardPanel.jsx`, RANKS button in `GameHud`. Checked by curl + screenshot (desktop/mobile); no pytest added. Ranking currently includes wallets created by earlier automated tests.
- UI test tip: announce a stub provider via `eip6963:announceProvider` (rdns `test.wallet`) + JWT in localStorage; a plain `window.ethereum` stub gets stuck in the MetaMask SDK.

- 2026-06: game token. Admin pastes a ponsfamily.com token link (`TokenForm.jsx`, `PUT/DELETE /api/admin/token`); backend parses the page's embedded `launch` JSON (name, symbol, artwork, price, market cap) and stores it in `settings` (key `token`). Public `GET /api/token` re-reads the page at most every 25s (falls back to last saved values); `MarketCapBadge.jsx` shows it top-centre in the game, polling every 30s. Checked by curl + screenshots (admin fetch, badge desktop/mobile); no pytest added. Depends on ponsfamily.com page markup: if they change it, fetch returns 422.

- 2026-06: real token payments + earnings + claims.
  - Buy: `POST /api/game/buy/quote {level}` prices the card (LEVELS price = USD) in the admin token at the live ponsfamily price (must be <5 min old), stores `game_orders`. Frontend `hooks/useBuyCard.js` reads balanceOf; if short it opens the token's ponsfamily link, else sends ERC-20 `transfer` to `TREASURY_ADDRESS` (backend/.env) and calls `POST /api/game/buy {order_id, tx_hash}`. Backend `find_transfer` checks the receipt over `RPC_URL` (token, sender = player, recipient = treasury, amount, block time within 15 min of the quote); `game_payments.tx_hash` unique stops reuse. 425 = not confirmed yet (frontend retries; unfinished payment kept in localStorage `potion.pendingbuy.<addr>` and resumed on next load).
  - Earnings: each placed, non-demo card earns price/1440 USD per minute (pays itself back in 24h). `game_players.earned_usd/earned_at`, `settle()` uses an optimistic lock on `earned_at`. Old `potion` fields and `demo: true` items earn nothing. HUD in game.html shows dollars.
  - Claim: `POST /api/game/claim` once per 12h, moves the whole balance into `game_claims` (token amount fixed at claim-time price). Admin `ClaimsPanel.jsx`: connect wallet in /admin, APPROVE & SEND transfers the token to the player, then `POST /api/admin/claims/{id}/paid {tx_hash}` verifies it on chain; REJECT returns the balance (cooldown stays).
  - Tested: backend pytest 37/37 (`backend/tests/test_wallet_game.py`), `find_transfer` checked against a real on-chain transfer, UI checked with a stub wallet (insufficient balance opens ponsfamily; claim + admin reject). NOT tested: a real funded purchase and a real admin payout (no funded wallet in preview).


## Backlog
- P2: tap-to-place on the map, selling/upgrading characters, claim history list for players.

## Known pitfalls
- Never `pip install siwe`. `signinwithethereum==5.1.0` already provides the `siwe` module; the PyPI `siwe==4.4.0` package overwrites it and crashes backend start with abnf 2.9.1 (all /api/* return 502). Fixed 2026-06 by removing it and re-freezing requirements.

## 2026-06 Update
- Treasury wallet changed to 0x608bEdEB69740e0330d79BaF55396CE7762A9B5E (backend/.env TREASURY_ADDRESS)

- 2026-06: NFT boost (OpenSea API v2, key in backend/.env `OPENSEA_API_KEY`, collected via secrets card).
  - Admin `NftForm.jsx`: paste an opensea.io collection (or single NFT) link -> `PUT /api/admin/nft` stores name, image, contracts, all string traits (`settings` key `nft`). Admin types a multiplier per trait value -> `PUT /api/admin/nft/boosts`. `DELETE /api/admin/nft` removes the collection and all boosts.
  - Player: `state.nft` lists the wallet's NFTs of that collection (count on the BOOST button in the HUD), each with its best multiplier (highest trait wins). `BoostPanel.jsx` + `POST /api/game/boost {item_id, nft|null}`: one NFT boosts one card, on/off. A 2x card earns 2x its daily USD.
  - Ownership: `load_holdings` re-reads the wallet from OpenSea when older than 5 min (`game_holdings`), always fresh on boost on/off, place, recall and claim. NFTs no longer owned are detached. `settle()` banks the boost extra only up to the last confirmed ownership check (`game_players.boost_at`), so a sold NFT earns nothing extra after its last check.
  - Tested: curl/script against real OpenSea data (Pudgy Penguins holder wallet, forged test JWT): fetch, trait save, boost on/off, 2x earnings over 1h, sold-NFT forfeiture; UI screenshots desktop + mobile. Existing pytest 37 pass (one quote test is flaky on token price). No pytest added for boost. Test collection removed afterwards: admin must add the real link.

- 2026-06: island expansion is paid in the game token ($2): `POST /api/game/expand/quote` -> ERC-20 transfer to treasury -> `POST /api/game/expand {order_id, tx_hash}` (same on-chain checks as card buys, shared `new_order`/`paid_order`; `game_orders.kind` = card|expansion). Frontend `useBuyCard.expand`. Pytest covers quote + rejected txs; a real funded expansion payment was NOT tested.
- NFT traits: `GET /api/admin/nft` re-reads traits from OpenSea when older than 60s (boost values kept); if OpenSea's trait summary is empty, traits are collected from the collection's NFT list. User's collection `potionrh` (chain robinhood, 0xf67e...2c2e) had total_supply 0 on 2026-11-14, so there were no traits to read yet.
- Pytest: run with `-n0`; under xdist the token delete/restore test races the quote tests.
- 2026-06: boost badge on the island. `GamePage` sends `boost` per placed item to `game.html`; `setBoost`/`boostBadge` add a bobbing green lightning "2X" sprite above the boosted unit (heights in `BADGE_Y`). Checked by screenshot with the `boost` field injected into the state response in the browser (user's collection has no NFTs yet); badge removal on boost-off not exercised.
