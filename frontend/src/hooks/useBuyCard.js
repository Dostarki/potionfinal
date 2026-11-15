import { useCallback, useEffect, useState } from "react";
import { erc20Abi, formatUnits } from "viem";
import { useReadContracts, useWriteContract } from "wagmi";
import { waitForTransactionReceipt } from "wagmi/actions";
import { toast } from "sonner";
import { robinhood, walletConfig } from "@/lib/wallet";
import { confirmBuy, confirmExpand, errorText, quoteBuy, quoteExpand } from "@/lib/gameApi";

const pendingKey = (address) => `potion.pendingbuy.${address.toLowerCase()}`;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function confirmWithRetry(jwt, pending) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await (pending.kind === "expansion" ? confirmExpand : confirmBuy)(jwt, pending.order_id, pending.tx_hash);
    } catch (err) {
      if (err?.response?.status !== 425 || attempt >= 10) throw err;
      await wait(3000);
    }
  }
}

export function useBuyCard({ session, token, onState }) {
  const [busy, setBusy] = useState(false);
  const { writeContractAsync } = useWriteContract();
  const erc20 = { abi: erc20Abi, address: token?.address, chainId: robinhood.id };
  const { data, refetch } = useReadContracts({
    contracts: [{ ...erc20, functionName: "balanceOf", args: [session.address] }, { ...erc20, functionName: "decimals" }],
    query: { enabled: Boolean(token?.address && session.address) },
  });
  const balance = data?.[0]?.result;
  const decimals = data?.[1]?.result;
  const tokenBalance = balance !== undefined && decimals !== undefined ? Number(formatUnits(balance, decimals)) : null;

  const openToken = useCallback(() => window.open(token.url, "_blank", "noopener"), [token]);
  const sendToToken = useCallback(() => {
    openToken();
    toast.error(`Not enough $${token.symbol} in your wallet`, { action: { label: "GET TOKEN", onClick: openToken } });
  }, [openToken, token]);

  const finish = useCallback(async (pending) => {
    const next = await confirmWithRetry(session.token, pending);
    localStorage.removeItem(pendingKey(session.address));
    onState(next);
    refetch();
    toast.success(pending.kind === "expansion" ? "Island expanded" : "Card bought. It is in your inventory");
    return next;
  }, [session.token, session.address, onState, refetch]);

  // A payment that was sent but not recorded (tab closed, network drop) is picked up again here.
  useEffect(() => {
    if (!session.ready) return;
    const saved = localStorage.getItem(pendingKey(session.address));
    if (!saved) return;
    finish(JSON.parse(saved)).catch((err) => {
      if (err?.response?.status !== 425) localStorage.removeItem(pendingKey(session.address));
      toast.error(errorText(err));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.ready, session.address]);

  const pay = async (kind, priceUsd, quote) => {
    if (!token) return toast.error("No game token is set yet");
    if (tokenBalance !== null && token.price_usd > 0 && tokenBalance < priceUsd / token.price_usd) return sendToToken();
    setBusy(true);
    try {
      const order = await quote();
      const amount = BigInt(order.amount_wei);
      const fresh = (await refetch()).data?.[0]?.result;
      if (fresh !== undefined && fresh < amount) return sendToToken();
      const txHash = await writeContractAsync({ abi: erc20Abi, address: order.token_address, functionName: "transfer", args: [order.treasury, amount], chainId: robinhood.id });
      const pending = { kind, order_id: order.id, tx_hash: txHash };
      localStorage.setItem(pendingKey(session.address), JSON.stringify(pending));
      toast.message("Payment sent, waiting for confirmation…");
      await waitForTransactionReceipt(walletConfig, { hash: txHash, chainId: robinhood.id });
      return await finish(pending);
    } catch (err) {
      const status = err?.response?.status;
      if (status === 401) session.signOut();
      if (status && status !== 425) localStorage.removeItem(pendingKey(session.address));
      toast.error(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const buy = (card) => pay("card", card.price, () => quoteBuy(session.token, card.level));
  const expand = (price) => pay("expansion", price, () => quoteExpand(session.token));

  return { buy, expand, busy, tokenBalance };
}
