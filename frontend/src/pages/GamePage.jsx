import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence } from "framer-motion";
import { toast } from "sonner";
import { useSession } from "@/hooks/useSession";
import { errorText, fetchState, gameAction, setBoost } from "@/lib/gameApi";
import { MainMenu } from "@/components/game/MainMenu";
import { GameHud } from "@/components/game/GameHud";
import { MarketPanel } from "@/components/game/MarketPanel";
import { InventoryPanel } from "@/components/game/InventoryPanel";
import { MarketCapBadge } from "@/components/game/MarketCapBadge";
import { LeaderboardPanel } from "@/components/game/LeaderboardPanel";
import { BoostPanel } from "@/components/game/BoostPanel";
import { useBuyCard } from "@/hooks/useBuyCard";

const DONE = { claim: "Claim sent. You get your tokens once it is approved", place: "Placed on the island", recall: "Back in your inventory" };

export default function GamePage() {
  const session = useSession();
  const queryClient = useQueryClient();
  const frame = useRef(null);
  const [frameReady, setFrameReady] = useState(false);
  const [panel, setPanel] = useState(null);
  const [busy, setBusy] = useState(false);
  const queryKey = ["game-state", session.address];

  const { data: state, error } = useQuery({ queryKey, queryFn: () => fetchState(session.token), enabled: session.ready, staleTime: 0 });

  const setState = useCallback((next) => queryClient.setQueryData(["game-state", session.address], next), [queryClient, session.address]);
  const shop = useBuyCard({ session, token: state?.token, onState: setState });

  const post = useCallback((message) => frame.current?.contentWindow?.postMessage(message, window.location.origin), []);

  useEffect(() => {
    const onMessage = (e) => {
      if (e.origin === window.location.origin && e.data?.type === "potion:ready") setFrameReady(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    if (error?.response?.status === 401 || error?.response?.status === 403) session.signOut();
  }, [error, session]);

  const live = session.ready ? state : null;
  useEffect(() => {
    if (!frameReady) return;
    post({
      type: "potion:state",
      placed: (live?.items || []).filter((i) => i.placed).map(({ id, level, slot, boost }) => ({ id, level, slot, boost })),
      potion: live?.earned_usd || 0,
      rate: (live?.per_min || 0) / 60,
      focus: live?.focus,
      plots: live?.expansion?.count || 0,
      focusPlot: live?.focus_plot,
    });
  }, [frameReady, live, post]);

  const act = async (action, level, nft) => {
    setBusy(true);
    try {
      const next = action === "boost" ? await setBoost(session.token, level, nft) : await gameAction(session.token, action, level);
      queryClient.setQueryData(queryKey, next);
      toast.success(action === "boost" ? (nft ? "Boost is on" : "Boost is off") : DONE[action]);
      if (action === "place") setPanel(null);
    } catch (err) {
      if (err?.response?.status === 401) session.signOut();
      toast.error(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const claim = () => {
    const wait = state.claim.next_at ? new Date(state.claim.next_at) - Date.now() : 0;
    if (wait > 0) {
      const last = state.claim.last;
      const note = last ? ` Last claim: $${last.amount_usd.toFixed(4)} (${last.status}).` : "";
      const mins = Math.ceil(wait / 60000);
      return toast.message(`Next claim in ${Math.floor(mins / 60)}h ${mins % 60}m.${note}`);
    }
    return act("claim");
  };

  const find = (id) => {
    post({ type: "potion:focus", id });
    setPanel(null);
  };

  return (
    <div className="game-shell" data-testid="game-page">
      <iframe ref={frame} data-testid="game-frame" title="PotionRH" src="/game.html" className="game-frame" />
      <MarketCapBadge />
      {!session.ready && <MainMenu session={session} />}
      {session.ready && state && (
        <>
          <GameHud address={session.address} owned={state.items.length} onMarket={() => setPanel("market")} onInventory={() => setPanel("inventory")} onLeaderboard={() => setPanel("leaderboard")} onBoost={() => setPanel("boost")} nfts={state.nft ? state.nft.nfts.length : null} busy={busy} onClaim={claim} />
          <AnimatePresence>
            {panel === "leaderboard" && <LeaderboardPanel key="leaderboard" token={session.token} onClose={() => setPanel(null)} />}
            {panel === "boost" && <BoostPanel key="boost" state={state} busy={busy} onBoost={(itemId, nft) => act("boost", itemId, nft)} onClose={() => setPanel(null)} />}
            {panel === "market" && <MarketPanel key="market" state={state} busy={busy || shop.busy} tokenBalance={shop.tokenBalance} onBuy={shop.buy} onClose={() => setPanel(null)} />}
            {panel === "inventory" && (
              <InventoryPanel
                key="inventory"
                state={state}
                busy={busy || shop.busy}
                onPlace={(lv) => act("place", lv)}
                onRecall={(lv) => act("recall", lv)}
                onExpand={async () => { const next = await shop.expand(state.expansion.price); if (next?.items) setPanel(null); }}
                onFind={find}
                onMarket={() => setPanel("market")}
                onClose={() => setPanel(null)}
              />
            )}
          </AnimatePresence>
        </>
      )}
    </div>
  );
}
