import { PixelSheet } from "@/components/game/PixelSheet";
import { CARD_INFO, perMin } from "@/components/game/cards";
import { formatTokens } from "@/lib/gameApi";

const MarketCard = ({ card, token, owned, busy, onBuy }) => (
  <article className="card" data-testid={`market-card-${card.level}`}>
    <div className="card-art">
      <img src={CARD_INFO[card.level].image} alt={card.name} loading="lazy" />
      <span className="card-level">LV {card.level}</span>
      {owned > 0 && <span className="card-owned" data-testid={`market-owned-${card.level}`}>x{owned}</span>}
    </div>
    <h3 className="card-name">{card.name}</h3>
    <p className="card-text">{CARD_INFO[card.level].text}</p>
    <p className="card-rate" data-testid={`market-rate-${card.level}`}>{perMin(card.per_min)} · ${card.price}/24H</p>
    {token?.price_usd > 0 && (
      <p className="card-token" data-testid={`market-token-cost-${card.level}`}>≈ {formatTokens(card.price / token.price_usd)} ${token.symbol}</p>
    )}
    <button type="button" className="px-btn px-btn-pink card-buy" data-testid={`buy-button-${card.level}`} disabled={busy || !token} onClick={() => onBuy(card)}>
      {busy ? "..." : "BUY"} <b data-testid={`market-price-${card.level}`}>${card.price}</b>
    </button>
  </article>
);

export const MarketPanel = ({ state, busy, tokenBalance, onBuy, onClose }) => {
  const token = state.token;
  const have = tokenBalance === null ? "" : ` · YOU HAVE ${formatTokens(tokenBalance)}`;
  return (
    <PixelSheet title="MARKET" badge={token ? `PAID IN $${token.symbol}${have}` : "NO GAME TOKEN SET YET"} testId="market-panel" onClose={onClose}>
      <div className="card-grid">
        {state.catalog.map((card) => (
          <MarketCard key={card.level} card={card} token={token} busy={busy} onBuy={onBuy} owned={state.items.filter((i) => i.level === card.level).length} />
        ))}
      </div>
    </PixelSheet>
  );
};
