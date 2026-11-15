import { useAccountModal } from "@rainbow-me/rainbowkit";
import { Backpack, Coins, Store, Trophy, Wallet, Zap } from "lucide-react";

export const GameHud = ({ address, owned, busy, onMarket, onInventory, onLeaderboard, onClaim, onBoost, nfts }) => {
  const { openAccountModal } = useAccountModal();
  return (
    <nav className="game-bar" data-testid="game-bar">
      <button type="button" className="px-btn px-btn-pink" data-testid="open-market-button" onClick={onMarket}>
        <Store size={16} strokeWidth={3} /> MARKET
      </button>
      <button type="button" className="px-btn px-btn-peach" data-testid="open-inventory-button" onClick={onInventory}>
        <Backpack size={16} strokeWidth={3} /> INVENTORY
        <span className="bar-count" data-testid="inventory-count">{owned}</span>
      </button>
      <button type="button" className="px-btn px-btn-mint" data-testid="claim-button" disabled={busy} onClick={onClaim}>
        <Coins size={16} strokeWidth={3} /> CLAIM
      </button>
      <button type="button" className="px-btn px-btn-ghost" data-testid="open-leaderboard-button" aria-label="Leaderboard" onClick={onLeaderboard}>
        <Trophy size={16} strokeWidth={3} /> <span className="bar-label">RANKS</span>
      </button>
      {nfts !== null && (
        <button type="button" className="px-btn px-btn-ghost" data-testid="open-boost-button" aria-label="NFT boost" onClick={onBoost}>
          <Zap size={16} strokeWidth={3} /> <span className="bar-label">BOOST</span>
          <span className="bar-count" data-testid="nft-count">{nfts}</span>
        </button>
      )}
      <button type="button" className="px-btn px-btn-ghost" data-testid="wallet-button" onClick={openAccountModal}>
        <Wallet size={16} strokeWidth={3} />
        <span className="bar-address" data-testid="wallet-address">{address.slice(0, 6)}…{address.slice(-4)}</span>
      </button>
    </nav>
  );
};
