import { useState } from "react";
import { PixelSheet } from "@/components/game/PixelSheet";

const cardLabel = (item, items) => {
  const n = items.filter((i) => i.level === item.level).indexOf(item) + 1;
  return `LV ${item.level} · ${item.name} #${n}${item.placed ? "" : " (inventory)"}`;
};

const NftRow = ({ nft, items, busy, onBoost }) => {
  const [pick, setPick] = useState("");
  const active = items.find((i) => i.boost_nft === nft.key);
  const free = items.filter((i) => !i.boost_nft && !i.demo);
  const canBoost = nft.multiplier > 1;
  return (
    <article className="inv-row boost-row" data-testid={`nft-row-${nft.identifier}`}>
      {nft.image && <img className="inv-art" src={nft.image} alt={nft.name} loading="lazy" />}
      <div className="inv-info">
        <h3 className="card-name">{nft.name}</h3>
        <p className="boost-line">
          <span className={`boost-tag${canBoost ? "" : " off"}`} data-testid={`nft-multiplier-${nft.identifier}`}>{canBoost ? `${nft.multiplier}X BOOST` : "NO BOOST"}</span>
          <span>{canBoost ? nft.trait : "None of its traits gives a boost."}</span>
        </p>
        {active && (
          <p className="boost-line" data-testid={`nft-active-${nft.identifier}`}>
            <span>ON: {cardLabel(active, items)} · ${active.price_usd * nft.multiplier}/24H instead of ${active.price_usd}</span>
            <button type="button" className="px-btn px-btn-peach" data-testid={`nft-off-${nft.identifier}`} disabled={busy} onClick={() => onBoost(active.id, null)}>TURN OFF</button>
          </p>
        )}
        {canBoost && !active && (
          <div className="boost-line">
            <select className="boost-select" data-testid={`nft-select-${nft.identifier}`} value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">{free.length ? "Choose a character…" : "No free character"}</option>
              {free.map((i) => <option key={i.id} value={i.id}>{cardLabel(i, items)}</option>)}
            </select>
            <button type="button" className="px-btn px-btn-mint" data-testid={`nft-on-${nft.identifier}`} disabled={busy || !pick} onClick={() => onBoost(pick, nft.key)}>TURN ON</button>
          </div>
        )}
      </div>
    </article>
  );
};

export const BoostPanel = ({ state, busy, onBoost, onClose }) => {
  const { nft, items } = state;
  return (
    <PixelSheet title="NFT BOOST" badge={`YOU OWN ${nft.nfts.length}`} testId="boost-panel" onClose={onClose}>
      <p className="boost-head">
        {nft.image && <img src={nft.image} alt="" />}
        <span>Each <a href={nft.url} target="_blank" rel="noreferrer" data-testid="boost-collection-link">{nft.name}</a> NFT in your wallet can boost one character. The boost stops if the NFT leaves your wallet.</span>
      </p>
      {!nft.checked && <p className="lb-note inv-full" data-testid="boost-unchecked">Could not read your NFTs from OpenSea right now. Try again in a minute.</p>}
      {nft.checked && nft.nfts.length === 0 && <p className="lb-note" data-testid="boost-empty">You have no {nft.name} NFT in this wallet.</p>}
      <div className="inv-list">
        {nft.nfts.map((n) => <NftRow key={n.key} nft={n} items={items} busy={busy} onBoost={onBoost} />)}
      </div>
    </PixelSheet>
  );
};
