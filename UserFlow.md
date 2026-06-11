# DeenCommerce Telegram Bot User Flow

This document outlines the interaction flow for customers using the DeenCommerce Telegram Bot.

## 1. Onboarding & Main Menu
- **User sends /start**:
  - Bot sends a welcoming "Assalamu Alaikum" message.
  - Presents the **Main Menu** with buttons:
    - 👔 **Categories**: Browse products by category hierarchy.
    - 🆕 **Latest Products**: View the newest additions.
    - 🔍 **Search**: Look up products by keyword.
    - 📦 **My Order**: Check the status of an existing order.
    - 🤖 **Ask DEEN AI Agent**: Start a conversation with the AI Shopping Assistant.
    - 🛍️ **View Cart**: Appears if items are in the cart.

## 2. Product Discovery
- **Browsing Categories**:
  - Users can drill down through parent and child categories.
  - Selecting a category shows a paginated list of products.
- **Searching**:
  - Users can type /search <keyword> or click the Search button.
  - The bot understands synonyms (e.g., "shart" for "shirt", "genji" for "t-shirt").
- **Product Details**:
  - Clicking a product shows its image, price, stock status, and description.
  - **📏 Size Chart**: If available, users can view a formatted size guide.
  - **🌐 View on Website**: Direct link to the product page.
  - **🛒 Add to Cart**: Adds the item to the in-bot shopping cart.

## 3. AI Shopping Assistant (Ask AI)
- **Entry**: Users click "Ask DEEN AI Agent" or use /ask <question>.
- **Capabilities**:
  - Ask about product recommendations ("Show me some blue shirts").
  - Inquire about store location or policies.
  - Natural language chat in English, Bangla, or Banglish.
- **Features**:
  - **🗑️ Reset Chat**: Clears conversation history to start fresh.
  - **Fallback**: If one AI provider fails, the bot automatically tries another to ensure a response.

## 4. Shopping Cart & Checkout
- **Add to Cart**:
  - **Simple Products**: Added directly to the in-bot cart.
  - **Variable Products (Sizes/Colors)**: Bot detects these and guides the user to the website to select specific options, as size/color selection is handled securely on the website.
- **Cart Management**:
  - Users can view their items and quantities.
  - Option to **🗑️ Empty Cart**.
- **Checkout**:
  - Clicking **💳 Checkout** generates a secure link to the DeenCommerce website.
  - For single items, the link automatically adds the item to the website cart and proceeds to checkout.
  - For multiple items, users are guided to the website cart to finalize their selection.

## 5. Order Tracking
- **Lookup**: Users provide their Order ID and Email/Phone.
- **Status**: Bot retrieves real-time status from WooCommerce.
- **Courier Tracking**: If the order is dispatched via Pathao or Steadfast, the bot provides a direct tracking link and current courier status.
