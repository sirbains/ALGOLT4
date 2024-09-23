import numpy as np
import requests
import signal
import time

# RTI variables
API_KEY = {'X-API-Key': 'MW0YJ28H'}
shutdown = False

# Signal handler for shutdown
def signal_handler(signum, frame):
    global shutdown
    shutdown = True

# Helper to get the current tick of the case
def get_tick(session):
    resp = session.get('http://localhost:9999/v1/case')
    if resp.status_code == 401:
        raise Exception('Invalid API Key.')
    case = resp.json()
    return case['tick']

# Fetch tender offers to evaluate
def get_tender_offer(session):
    tender_resp = session.get('http://localhost:9999/v1/tenders')
    tender_offer = tender_resp.json()
    if tender_offer:
        print(f"Tender Offer Retrieved: {tender_offer}")  # Print the tender offer
        return tender_offer[0]  # Return the first tender offer for evaluation
    return None

# Accept a fixed-price tender offer
def accept_tender(session, tender_id):
    resp = session.post(f'http://localhost:9999/v1/tenders/{tender_id}', params={'accept': True})
    if resp.status_code == 200:
        print(f"Tender {tender_id} Accepted")
    else:
        print(f"Failed to accept tender {tender_id}: {resp.status_code}, {resp.text}")

# Get the current order book for both main and alternative markets
def get_order_books(session, ticker_main, ticker_alt):
    main_resp = session.get(f'http://localhost:9999/v1/securities/book?ticker={ticker_main}')
    alt_resp = session.get(f'http://localhost:9999/v1/securities/book?ticker={ticker_alt}')
    main_book = main_resp.json()
    alt_book = alt_resp.json()
    
    # Get the best bid and ask prices for both markets
    main_best_bid = main_book['bids'][0]['price'] if main_book['bids'] else 0
    main_best_ask = main_book['asks'][0]['price'] if main_book['asks'] else 0
    alt_best_bid = alt_book['bids'][0]['price'] if alt_book['bids'] else 0
    alt_best_ask = alt_book['asks'][0]['price'] if alt_book['asks'] else 0

    return main_best_bid, main_best_ask, alt_best_bid, alt_best_ask

# Calculate the Average True Range (ATR) for volatility-based threshold
def calculate_atr(prices, window=10):
    if len(prices) < window:
        return 0.10  # Default threshold if not enough data
    high_low = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return np.mean(high_low[-window:])

# Unwind the position after a tender offer is accepted with market selection logic
def unwind_position(session, ticker, ticker_alt, volume, action):
    """
    Unwind the position after accepting a tender offer. 
    
    - If action is "BUY" (we bought shares from the institution), we will SELL shares to unwind.
    - If action is "SELL" (we sold shares to the institution), we will BUY shares to unwind.
    
    This function ensures we choose the best market for unwinding based on highest bid or lowest ask.
    """
    unwind_action = "SELL" if action == "BUY" else "BUY"
    
    while volume > 0:
        order_size = min(10000, volume)  # Unwind in blocks of 10,000 shares

        try:
            # Compare prices between the main and alternative market
            main_best_bid, main_best_ask, alt_best_bid, alt_best_ask = get_order_books(session, ticker, ticker_alt)

            # Determine the best market for the unwind action
            if unwind_action == "SELL":
                # Sell at the highest bid
                if main_best_bid >= alt_best_bid:
                    # Place sell order in the main market
                    resp = session.post(f'http://localhost:9999/v1/orders', params={
                        'ticker': ticker,
                        'type': 'MARKET',
                        'quantity': order_size,
                        'action': 'SELL'
                    })
                    print(f"Market Sell Order Placed for {ticker}: {order_size} shares at Main Market (Bid: {main_best_bid}), Response: {resp.status_code}, {resp.text}")
                else:
                    # Place sell order in the alternative market
                    resp = session.post(f'http://localhost:9999/v1/orders', params={
                        'ticker': ticker_alt,
                        'type': 'MARKET',
                        'quantity': order_size,
                        'action': 'SELL'
                    })
                    print(f"Market Sell Order Placed for {ticker_alt}: {order_size} shares at Alternative Market (Bid: {alt_best_bid}), Response: {resp.status_code}, {resp.text}")
            
            elif unwind_action == "BUY":
                # Buy at the lowest ask
                if main_best_ask <= alt_best_ask:
                    # Place buy order in the main market
                    resp = session.post(f'http://localhost:9999/v1/orders', params={
                        'ticker': ticker,
                        'type': 'MARKET',
                        'quantity': order_size,
                        'action': 'BUY'
                    })
                    print(f"Market Buy Order Placed for {ticker}: {order_size} shares at Main Market (Ask: {main_best_ask}), Response: {resp.status_code}, {resp.text}")
                else:
                    # Place buy order in the alternative market
                    resp = session.post(f'http://localhost:9999/v1/orders', params={
                        'ticker': ticker_alt,
                        'type': 'MARKET',
                        'quantity': order_size,
                        'action': 'BUY'
                    })
                    print(f"Market Buy Order Placed for {ticker_alt}: {order_size} shares at Alternative Market (Ask: {alt_best_ask}), Response: {resp.status_code}, {resp.text}")
            
            # Reduce volume by the successfully placed order size
            volume -= order_size

        except Exception as e:
            print(f"Error during order placement: {e}")
            break
        
        time.sleep(1)  # Short delay between each block

# Main function to automatically handle tender offers and unwind positions
def main():
    with requests.Session() as s:
        s.headers.update(API_KEY)

        tick = get_tick(s)
        print(f"Starting at Tick: {tick}")

        price_history = []

        while not shutdown:
            # Get the current tender offer
            tender_offer = get_tender_offer(s)
            if tender_offer:
                tender_id = tender_offer['tender_id']
                volume = tender_offer['quantity']
                ticker = tender_offer['ticker']
                action = tender_offer['action']  # BUY or SELL
                is_fixed_bid = tender_offer.get('is_fixed_bid', False)
                tender_price = tender_offer['price']  # May be None

                # Alternative market ticker
                ticker_alt = ticker.replace('_M', '_A')

                # Get current best prices
                main_best_bid, main_best_ask, alt_best_bid, alt_best_ask = get_order_books(s, ticker, ticker_alt)

                # Use best bid and best ask for decision making
                market_price = (main_best_bid + main_best_ask) / 2

                # Add market price to history for ATR calculation
                price_history.append(market_price)
                threshold = calculate_atr(price_history, window=10)  # Use ATR for volatility-based threshold

                if is_fixed_bid:
                    # Fixed-price tender, decide to accept or decline
                    if action == "BUY" and tender_price < main_best_ask - threshold:
                        print(f"Tender Decision: accept, Tender Price: {tender_price}, Market Price: {main_best_ask}")
                        accept_tender(s, tender_id)
                        unwind_position(s, ticker, ticker_alt, volume, action)
                    elif action == "SELL" and tender_price > main_best_bid + threshold:
                        print(f"Tender Decision: accept, Tender Price: {tender_price}, Market Price: {main_best_bid}")
                        accept_tender(s, tender_id)
                        unwind_position(s, ticker, ticker_alt, volume, action)
                    else:
                        print(f"Tender Decision: decline, Tender Price: {tender_price}, Market Price: {market_price}")
                else:
                    # Winner-take-all tender, need to submit a bid/offer
                    print("Winner-take-all tender handling not implemented in this version.")
                    
            time.sleep(2)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    main()

