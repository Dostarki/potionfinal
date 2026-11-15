import axios from "axios";

export const api = axios.create({ baseURL: `${process.env.REACT_APP_BACKEND_URL}/api` });

const auth = (token) => ({ headers: { Authorization: `Bearer ${token}` } });

export const fetchNonce = (address) => api.get("/auth/nonce", { params: { address } }).then((r) => r.data.nonce);
export const verifyWallet = (message, signature) => api.post("/auth/verify", { message, signature }).then((r) => r.data);
export const fetchState = (token) => api.get("/game/state", auth(token)).then((r) => r.data);
export const fetchLeaderboard = (token) => api.get("/game/leaderboard", auth(token)).then((r) => r.data);
export const fetchToken = () => api.get("/token").then((r) => r.data.token);
export const formatUsd = (n) => (typeof n === "number" ? `$${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(n)}` : "—");
export const formatTokens = (n) => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(n);
export const quoteBuy = (token, level) => api.post("/game/buy/quote", { level }, auth(token)).then((r) => r.data);
export const confirmBuy = (token, order_id, tx_hash) => api.post("/game/buy", { order_id, tx_hash }, auth(token)).then((r) => r.data);
export const quoteExpand = (token) => api.post("/game/expand/quote", {}, auth(token)).then((r) => r.data);
export const confirmExpand = (token, order_id, tx_hash) => api.post("/game/expand", { order_id, tx_hash }, auth(token)).then((r) => r.data);
export const gameAction = (token, action, level) => api.post(`/game/${action}`, level ? { level } : {}, auth(token)).then((r) => r.data);
export const setBoost = (token, item_id, nft) => api.post("/game/boost", { item_id, nft }, auth(token)).then((r) => r.data);

export const errorText = (err) => err?.response?.data?.detail || err?.shortMessage || err?.message || "Something went wrong";
