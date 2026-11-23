/**
 * `lib/rates.ts` 的测试（`npm test`；Node 内置的 `node:test`，不引测试框架）。
 *
 * 前端此前没有任何自动化测试 —— 窄条的判断逻辑（陈旧、加息/降息文案、倒计时、利差）
 * 若只放在组件里，就只能靠肉眼验收。所以逻辑全在 `rates.ts` 里，这里逐条断言。
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  buildModel,
  changeText,
  isStale,
  meetingText,
  rangeText,
  signedBp,
  spread2s10s,
  yieldCells,
  type MeetingRow,
  type RatePolicyRow,
  type RatesInput,
  type YieldRow,
} from "./rates.ts";

const TODAY = "2026-11-23";
const TENORS = [
  { key: "3m", label: "3M" },
  { key: "2y", label: "2Y" },
  { key: "10y", label: "10Y" },
];
const STALE = { target_range: 7, yields: 7, fomc: 14 };

const LATEST: RatePolicyRow = {
  date: "2026-11-19",
  target_lower: 3.75,
  target_upper: 4.0,
  effr: 3.88,
  change_kind: null,
  change_bp: null,
};
const MEETING: MeetingRow = {
  start_date: "2026-12-08",
  end_date: "2026-12-09",
  fetched_at: "2026-11-23T14:35:00+00:00",
};
const Y = (date: string, tenor: string, v: number): YieldRow => ({ date, tenor, yield_pct: v });

function input(over: Partial<RatesInput> = {}): RatesInput {
  return {
    latest: LATEST,
    earliest: "2022-01-03",
    lastChange: { date: "2026-09-17", change_kind: "up", change_bp: 25 },
    yields: [
      Y("2026-11-20", "3m", 4.32),
      Y("2026-11-20", "2y", 4.63),
      Y("2026-11-20", "10y", 4.92),
      Y("2026-11-19", "3m", 4.34),
      Y("2026-11-19", "2y", 4.76),
      Y("2026-11-19", "10y", 5.04),
    ],
    nextMeeting: MEETING,
    ...over,
  };
}

describe("isStale", () => {
  test("no data at all is stale", () => assert.equal(isStale(null, TODAY, 7), true));
  test("exactly at the threshold is not stale; one day past is", () => {
    assert.equal(isStale("2026-11-16", TODAY, 7), false); // 7 天
    assert.equal(isStale("2026-11-15", TODAY, 7), true); // 8 天
  });
  test("a long weekend (Fri → Tue, 4 days) never trips the 7-day rule", () => {
    assert.equal(isStale("2026-11-20", "2026-11-24", 7), false);
  });
});

describe("changeText", () => {
  test("a hike and a cut read as such, with unsigned magnitude", () => {
    assert.equal(changeText("up", 25), "加息 25bp");
    assert.equal(changeText("down", -50), "降息 50bp");
  });
  test("mixed guesses neither direction nor size", () => {
    const t = changeText("mixed", null);
    assert.match(t, /方向未知/);
    assert.doesNotMatch(t, /加息|降息|bp/);
  });
  test("a direction with a missing magnitude falls back to unknown rather than printing 'NaNbp'", () => {
    assert.match(changeText("up", null), /方向未知/);
  });
});

describe("meetingText", () => {
  test("same-month range", () => assert.equal(meetingText(MEETING), "12-08 ~ 12-09"));
  test("cross-month range", () => {
    assert.equal(
      meetingText({ start_date: "2023-01-31", end_date: "2023-02-01", fetched_at: "" }),
      "01-31 ~ 02-01",
    );
  });
  test("a one-day meeting prints one date", () => {
    assert.equal(
      meetingText({ start_date: "2026-12-09", end_date: "2026-12-09", fetched_at: "" }),
      "12-09",
    );
  });
});

describe("yieldCells", () => {
  test("latest value, and the change against that tenor's own previous point", () => {
    const cells = yieldCells(input().yields, TENORS);
    const two = cells.find((c) => c.key === "2y");
    assert.equal(two?.value, 4.63);
    assert.equal(two?.changeBp, -13); // 4.63 − 4.76
  });
  test("a tenor missing on the newest day uses its own last point, not another tenor's date", () => {
    const rows = [
      Y("2026-11-20", "2y", 4.63),
      Y("2026-11-20", "10y", 4.92),
      Y("2026-11-19", "3m", 4.34),
      Y("2026-11-18", "3m", 4.3),
    ];
    const three = yieldCells(rows, TENORS).find((c) => c.key === "3m");
    assert.deepEqual([three?.date, three?.value, three?.changeBp], ["2026-11-19", 4.34, 4]);
  });
  test("a tenor with no rows is omitted, never drawn as zero", () => {
    const cells = yieldCells([Y("2026-11-20", "2y", 4.63)], TENORS);
    assert.deepEqual(
      cells.map((c) => c.key),
      ["2y"],
    );
  });
  test("a single point has no change", () => {
    assert.equal(yieldCells([Y("2026-11-20", "2y", 4.63)], TENORS)[0]?.changeBp, null);
  });
  test("a change that rounds to zero is 0, not null", () => {
    const rows = [Y("2026-11-20", "2y", 4.63), Y("2026-11-19", "2y", 4.63)];
    assert.equal(yieldCells(rows, TENORS)[0]?.changeBp, 0);
  });
  test("bp rounding is exact despite float subtraction (4.35 − 4.10)", () => {
    const rows = [Y("2026-11-20", "2y", 4.35), Y("2026-11-19", "2y", 4.1)];
    assert.equal(yieldCells(rows, TENORS)[0]?.changeBp, 25);
  });
});

describe("spread2s10s", () => {
  test("10Y − 2Y in bp when both are from the same day", () => {
    assert.equal(spread2s10s(yieldCells(input().yields, TENORS)), 29); // 4.92 − 4.63
  });
  test("is null when the two tenors are from different days", () => {
    const cells = yieldCells([Y("2026-11-20", "10y", 4.92), Y("2026-11-19", "2y", 4.76)], TENORS);
    assert.equal(spread2s10s(cells), null);
  });
  test("is null when either tenor is missing", () => {
    assert.equal(spread2s10s(yieldCells([Y("2026-11-20", "10y", 4.92)], TENORS)), null);
  });
  test("an inverted curve is negative", () => {
    const cells = yieldCells([Y("2026-11-20", "10y", 4.0), Y("2026-11-20", "2y", 4.5)], TENORS);
    assert.equal(spread2s10s(cells), -50);
  });
});

describe("buildModel", () => {
  test("a healthy day", () => {
    const m = buildModel(input(), TENORS, STALE, TODAY);
    assert.deepEqual(m.range, { lower: 3.75, upper: 4.0, date: "2026-11-19", stale: false });
    assert.deepEqual(m.lastChange, {
      text: "加息 25bp",
      kind: "up",
      date: "2026-09-17",
      daysAgo: 67,
    });
    assert.equal(m.meeting?.text, "12-08 ~ 12-09");
    assert.equal(m.meeting?.daysUntil, 15); // 11-23 → 开始日 12-08，与设计文档 §3 的例子一致
    assert.equal(m.meeting?.inProgress, false);
    assert.equal(m.meeting?.stale, false);
    assert.equal(m.noChangeSince, null); // 有变动时不写「无变动」
    assert.equal(m.yields?.stale, false);
    assert.equal(m.yields?.spread2s10sBp, 29);
  });

  test("no change ever recorded: says 'since <earliest stored date>', not the configured start", () => {
    const m = buildModel(input({ lastChange: null }), TENORS, STALE, TODAY);
    assert.equal(m.lastChange, null);
    assert.equal(m.noChangeSince, "2022-01-03");
  });

  test("with no target-range data it must NOT claim 'no change' — that would be inventing a fact", () => {
    // 宏观管道首次回填时 nyfed 失败、而 treasury / fomc 成功：目标区间表为空。
    const m = buildModel(input({ latest: null, lastChange: null, earliest: null }), TENORS, STALE, TODAY);
    assert.equal(m.range, null);
    assert.equal(m.noChangeSince, null);
  });

  test("a table with a single row says 'since that row', never an earlier date", () => {
    const m = buildModel(input({ lastChange: null, earliest: LATEST.date }), TENORS, STALE, TODAY);
    assert.equal(m.noChangeSince, LATEST.date);
  });

  test("no upcoming meeting: null, not a large number", () => {
    assert.equal(buildModel(input({ nextMeeting: null }), TENORS, STALE, TODAY).meeting, null);
  });

  test("each part goes stale on its own clock", () => {
    const m = buildModel(
      input({
        latest: { ...LATEST, date: "2026-11-10" }, // 13 天前 > 7
        yields: [Y("2026-11-20", "2y", 4.63)], // 3 天前
        nextMeeting: { ...MEETING, fetched_at: "2026-11-01T00:00:00+00:00" }, // 22 天前 > 14
      }),
      TENORS,
      STALE,
      TODAY,
    );
    assert.equal(m.range?.stale, true);
    assert.equal(m.yields?.stale, false);
    assert.equal(m.meeting?.stale, true);
  });

  test("the countdown runs to the START date; on day one the meeting is 'in progress', not '1 day away'", () => {
    const at = (today: string) => buildModel(input(), TENORS, STALE, today).meeting;
    assert.equal(at("2026-12-07")?.daysUntil, 1);
    assert.equal(at("2026-12-07")?.inProgress, false);
    assert.equal(at("2026-12-08")?.inProgress, true); // 会议第一天
    assert.equal(at("2026-12-08")?.daysUntil, 0);
  });

  test("an empty yields table yields no yields block", () => {
    assert.equal(buildModel(input({ yields: [] }), TENORS, STALE, TODAY).yields, null);
  });

  test("an empty rates table yields no range block", () => {
    assert.equal(buildModel(input({ latest: null }), TENORS, STALE, TODAY).range, null);
  });

  test("a mixed change is reported as such and keeps its date", () => {
    const m = buildModel(
      input({ lastChange: { date: "2026-10-01", change_kind: "mixed", change_bp: null } }),
      TENORS,
      STALE,
      TODAY,
    );
    assert.equal(m.lastChange?.kind, "mixed");
    assert.match(m.lastChange?.text ?? "", /方向未知/);
  });
});

describe("number formatting", () => {
  test("signedBp uses the true minus sign U+2212, like lib/format.ts", () => {
    assert.equal(signedBp(25), "+25bp");
    assert.equal(signedBp(-13), "−13bp");
    assert.equal(signedBp(0), "0bp");
  });
  test("rangeText always shows two decimals and an en dash", () => {
    assert.equal(rangeText(3.75, 4), "3.75–4.00%");
    assert.equal(rangeText(0, 0.25), "0.00–0.25%"); // 零利率时期
  });
});
