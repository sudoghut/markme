// 临时探针：验证 gitleaks-action v3 升级之后**告警还会响**。
// 下面这一行是上一轮被 v2 的 generic-api-key 规则命中的同一个模式
// （一个字段名后面跟一个长指标 id，entropy 3.57）—— 它不是任何凭证。
// 这个文件只活在 probe-gitleaks-v3 这条分支上，验完即删。
export const probe = { key: "close_vs_ema60_pct", label: "gitleaks probe" };
