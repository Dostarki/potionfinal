import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api, errorText } from "@/lib/gameApi";

const keyOf = (t) => `${t.type}\u0000${t.value}`;
const toValues = (nft) => Object.fromEntries((nft?.boosts || []).map((b) => [keyOf(b), String(b.multiplier)]));

const TraitGroup = ({ type, traits, values, onChange }) => {
  const set = traits.filter((t) => Number(values[keyOf(t)]) > 1).length;
  return (
    <details className="trait-group" data-testid={`trait-group-${type}`} open>
      <summary>{type} <small>{traits.length} values{set ? ` · ${set} boosted` : ""}</small></summary>
      {traits.map((t) => (
        <label className="trait-row" key={keyOf(t)}>
          <span>{t.value}</span>
          <small>{t.count} NFTs</small>
          <input className="pixel-input" type="number" min="1" max="100" step="0.1" placeholder="1x" data-testid={`trait-boost-${t.type}-${t.value}`} value={values[keyOf(t)] || ""} onChange={(e) => onChange(keyOf(t), e.target.value)} />
        </label>
      ))}
    </details>
  );
};

export const NftForm = ({ auth }) => {
  const [url, setUrl] = useState("");
  const [nft, setNft] = useState(null);
  const [values, setValues] = useState({});
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);

  const load = (data) => { setNft(data.nft); setValues(toValues(data.nft)); setUrl(data.nft?.url || ""); };
  useEffect(() => { api.get("/admin/nft", auth).then((r) => r.data.nft && load(r.data)).catch(() => {}); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const run = async (request, done) => {
    setBusy(true);
    try { load((await request()).data); toast.success(done); }
    catch (err) { toast.error(errorText(err)); }
    finally { setBusy(false); }
  };
  const submit = (e) => { e.preventDefault(); run(() => api.put("/admin/nft", { url }, auth), "Collection and traits fetched"); };
  const save = () => {
    const boosts = nft.traits.filter((t) => Number(values[keyOf(t)]) > 1).map((t) => ({ type: t.type, value: t.value, multiplier: Number(values[keyOf(t)]) }));
    run(() => api.put("/admin/nft/boosts", { boosts }, auth), "Boosts saved");
  };

  const groups = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const out = {};
    (nft?.traits || []).filter((t) => !q || `${t.type} ${t.value}`.toLowerCase().includes(q)).forEach((t) => { (out[t.type] ||= []).push(t); });
    return Object.entries(out);
  }, [nft, filter]);

  return (
    <form className="pixel-card" onSubmit={submit} data-testid="nft-form">
      <h2 className="pixel-h2">NFT boost</h2>
      <label className="pixel-label" htmlFor="nft-url">OPENSEA COLLECTION LINK</label>
      <div className="token-row">
        <input id="nft-url" data-testid="nft-url-input" className="pixel-input" type="url" placeholder="https://opensea.io/collection/..." value={url} onChange={(e) => setUrl(e.target.value)} required />
        <button className="pixel-btn" type="submit" disabled={busy} data-testid="nft-fetch-button">{busy ? "..." : "FETCH"}</button>
      </div>
      <p className="hint">Players holding an NFT of this collection can boost one character per NFT. Enter a multiplier (2 = 2x earnings) next to each trait that should give a boost; leave the rest empty.</p>
      {nft && (
        <>
          <div className="token-info" data-testid="nft-info">
            {nft.image && <img src={nft.image} alt="" />}
            <dl>
              <dt>Collection</dt><dd data-testid="nft-name">{nft.name}</dd>
              <dt>Supply</dt><dd data-testid="nft-supply">{nft.total_supply ?? "—"}</dd>
              <dt>Contract</dt><dd data-testid="nft-contract">{nft.contracts.map((c) => `${c.chain}: ${c.address}`).join(", ")}</dd>
            </dl>
            <button className="pixel-btn ghost" type="button" disabled={busy} data-testid="nft-remove-button" onClick={() => run(() => api.delete("/admin/nft", auth), "Collection removed")}>REMOVE</button>
          </div>
          {nft.traits.length > 12 && <input className="pixel-input trait-filter" type="search" placeholder="Search traits (legendary, gold...)" data-testid="trait-filter-input" value={filter} onChange={(e) => setFilter(e.target.value)} />}
          {nft.traits.length === 0 && <p className="hint" data-testid="nft-no-traits">{nft.total_supply ? "OpenSea lists no traits for this collection yet." : "No NFT of this collection is minted yet, so there are no traits to read."} They show up here by themselves once the NFTs are minted and revealed.</p>}
          {groups.map(([type, traits]) => <TraitGroup key={type} type={type} traits={traits} values={values} onChange={(k, v) => setValues((old) => ({ ...old, [k]: v }))} />)}
          <div className="trait-save">
            <button className="pixel-btn" type="button" disabled={busy} data-testid="nft-boosts-save" onClick={save}>SAVE BOOSTS</button>
            <span className="hint" data-testid="nft-boost-count">{Object.values(values).filter((v) => Number(v) > 1).length} traits give a boost</span>
          </div>
        </>
      )}
    </form>
  );
};
