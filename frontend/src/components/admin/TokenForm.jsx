import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, errorText, fetchToken, formatUsd } from "@/lib/gameApi";

export const TokenForm = ({ auth }) => {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { fetchToken().then((t) => { setToken(t); setUrl(t?.url || ""); }).catch(() => {}); }, []);

  const run = async (request, done) => {
    setBusy(true);
    try { const { data } = await request(); setToken(data.token); if (!data.token) setUrl(""); toast.success(done); }
    catch (err) { toast.error(errorText(err)); }
    finally { setBusy(false); }
  };
  const submit = (e) => { e.preventDefault(); run(() => api.put("/admin/token", { url }, auth), "Token details fetched"); };

  return (
    <form className="pixel-card" onSubmit={submit} data-testid="token-form">
      <h2 className="pixel-h2">Game token</h2>
      <label className="pixel-label" htmlFor="token-url">PONSFAMILY.COM TOKEN LINK</label>
      <div className="token-row">
        <input id="token-url" data-testid="token-url-input" className="pixel-input" type="url" placeholder="https://ponsfamily.com/launchpad/0x..." value={url} onChange={(e) => setUrl(e.target.value)} required />
        <button className="pixel-btn" type="submit" disabled={busy} data-testid="token-fetch-button">{busy ? "..." : "FETCH"}</button>
      </div>
      <p className="hint">Paste the token page link. Its market cap is shown at the top of the game and refreshed every 30 seconds.</p>
      {token && (
        <div className="token-info" data-testid="token-info">
          {token.image && <img src={token.image} alt="" />}
          <dl>
            <dt>Token</dt><dd data-testid="token-name">{token.name} (${token.symbol})</dd>
            <dt>Market cap</dt><dd data-testid="token-market-cap">{formatUsd(token.market_cap_usd)}</dd>
            <dt>Price</dt><dd data-testid="token-price">{typeof token.price_usd === "number" ? `$${token.price_usd.toPrecision(4)}` : "—"}</dd>
            <dt>Contract</dt><dd data-testid="token-address">{token.address}</dd>
          </dl>
          <button className="pixel-btn ghost" type="button" disabled={busy} data-testid="token-remove-button" onClick={() => run(() => api.delete("/admin/token", auth), "Token removed")}>REMOVE</button>
        </div>
      )}
    </form>
  );
};
