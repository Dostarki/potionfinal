import { useCallback, useEffect, useState } from "react";
import { ConnectButton } from "@rainbow-me/rainbowkit";
import { erc20Abi, formatUnits } from "viem";
import { useAccount, useWriteContract } from "wagmi";
import { waitForTransactionReceipt } from "wagmi/actions";
import { toast } from "sonner";
import { robinhood, walletConfig } from "@/lib/wallet";
import { api, errorText, formatTokens } from "@/lib/gameApi";

const txKey = (id) => `potion.claimtx.${id}`;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const explorer = robinhood.blockExplorers.default.url;

async function markPaid(id, txHash, auth) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await api.post(`/admin/claims/${id}/paid`, { tx_hash: txHash }, auth);
    } catch (err) {
      if (err?.response?.status !== 425 || attempt >= 10) throw err;
      await wait(3000);
    }
  }
}

const ClaimRow = ({ claim, busy, canPay, onPay, onReject }) => {
  const sent = claim.status === "pending" && localStorage.getItem(txKey(claim.id));
  const tx = claim.tx_hash || sent;
  return (
    <div className="claim-row" data-testid={`claim-row-${claim.id}`}>
      <div className="claim-main">
        <b data-testid={`claim-amount-${claim.id}`}>{formatTokens(Number(formatUnits(BigInt(claim.amount_wei), claim.decimals)))} ${claim.symbol} · ${claim.amount_usd.toFixed(4)}</b>
        <a href={`${explorer}/address/${claim.address}`} target="_blank" rel="noreferrer" data-testid={`claim-address-${claim.id}`}>{claim.address}</a>
        <div className="muted">
          {new Date(claim.created_at).toLocaleString()}
          {tx && <> · <a href={`${explorer}/tx/${tx}`} target="_blank" rel="noreferrer">transaction</a></>}
        </div>
      </div>
      <span className={`claim-status ${claim.status}`} data-testid={`claim-status-${claim.id}`}>{claim.status.toUpperCase()}</span>
      {claim.status === "pending" && (
        <>
          <button type="button" className="pixel-btn" data-testid={`claim-approve-${claim.id}`} disabled={busy || (!sent && !canPay)} onClick={() => onPay(claim)}>
            {sent ? "VERIFY PAYMENT" : "APPROVE & SEND"}
          </button>
          {!sent && <button type="button" className="pixel-btn ghost" data-testid={`claim-reject-${claim.id}`} disabled={busy} onClick={() => onReject(claim)}>REJECT</button>}
        </>
      )}
    </div>
  );
};

export const ClaimsPanel = ({ auth }) => {
  const [claims, setClaims] = useState([]);
  const [busy, setBusy] = useState(false);
  const { isConnected, chainId } = useAccount();
  const { writeContractAsync } = useWriteContract();
  const canPay = isConnected && chainId === robinhood.id;

  const load = useCallback(() => api.get("/admin/claims", auth).then((r) => setClaims(r.data.claims)).catch((err) => toast.error(errorText(err))), [auth.headers.Authorization]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [load]);

  const run = async (work, done) => {
    setBusy(true);
    try { await work(); toast.success(done); }
    catch (err) { toast.error(errorText(err)); }
    finally { setBusy(false); load(); }
  };

  const pay = (claim) => run(async () => {
    let txHash = localStorage.getItem(txKey(claim.id));
    if (!txHash) {
      txHash = await writeContractAsync({ abi: erc20Abi, address: claim.token_address, functionName: "transfer", args: [claim.address, BigInt(claim.amount_wei)], chainId: robinhood.id });
      localStorage.setItem(txKey(claim.id), txHash);
      await waitForTransactionReceipt(walletConfig, { hash: txHash, chainId: robinhood.id });
    }
    await markPaid(claim.id, txHash, auth);
    localStorage.removeItem(txKey(claim.id));
  }, "Claim paid");

  const reject = (claim) => run(() => api.post(`/admin/claims/${claim.id}/reject`, {}, auth), "Claim rejected, balance returned to the player");
  const pending = claims.filter((c) => c.status === "pending").length;

  return (
    <section className="pixel-card" data-testid="claims-panel">
      <div className="claims-head">
        <h2 className="pixel-h2">Claim requests <span className="count" data-testid="claims-pending-count">{pending}</span></h2>
        <ConnectButton chainStatus="icon" showBalance={false} accountStatus="address" />
      </div>
      <p className="hint">Connect the wallet that pays players. Approving a claim sends the tokens from that wallet to the player.</p>
      {claims.length === 0 && <p className="muted" data-testid="claims-empty">No claim requests yet</p>}
      {claims.map((claim) => <ClaimRow key={claim.id} claim={claim} busy={busy} canPay={canPay} onPay={pay} onReject={reject} />)}
    </section>
  );
};
