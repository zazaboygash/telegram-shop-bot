# External VPN promo-code UX

The Telegram adapter reads the live Gash Core catalog through `GashCoreClient`; it does not open a local commerce database, keep code inventory, or accept credentials.

- `/vpn_codes` (also `/vpn`, `/promocodes`, `/promo`) presents active Core variants in 1/3/6/12-month and balance-code sections.
- Prices and currency are rendered only when supplied by Core. Missing values are described as Mini App pricing; unknown prices are not guessed.
- The bot renders catalog titles and order instructions only. It never renders a fulfillment code, secret, credential, or inventory payload.
- A customer buys in the shared Mini App. Core owns the order and fulfillment. After fulfillment, order details are the delivery point for setup instructions, the bot link, and the code.
- The polling runner registers `/start`, `/vpn_codes`, `/store`, `/accounts`, `/profile`, `/orders`, and `/help` with Telegram using `setMyCommands`.

This is a presentation layer only: Core remains the sole source of catalog, wallet, order, and fulfillment state.
