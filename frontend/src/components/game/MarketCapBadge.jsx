import { useQuery } from "@tanstack/react-query";
import { fetchToken, formatUsd } from "@/lib/gameApi";

export const MarketCapBadge = () => {
  const { data: token } = useQuery({ queryKey: ["token"], queryFn: fetchToken, refetchInterval: 30000 });
  if (!token) return null;
  return (
    <a className="mcap-badge" data-testid="market-cap-badge" href={token.url} target="_blank" rel="noreferrer">
      {token.image && <img src={token.image} alt="" />}
      <span>
        <span className="mcap-label" data-testid="market-cap-symbol">${token.symbol} MARKET CAP</span>
        <span className="mcap-value" data-testid="market-cap-value">{formatUsd(token.market_cap_usd)}</span>
      </span>
    </a>
  );
};
