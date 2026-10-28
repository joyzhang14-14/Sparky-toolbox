// 控制台的前端（任务、运行记录、日志、工具、子代理、API，页面顶上的运行条与左下的服务灯）：共用的小工具（$、esc、api、时长与时间的写法、状态标签…）也在这里，聊天跟着用。
// 宿主页面（channels/web/frontend/index.html）先引用 console.css，再依次加载本文件、聊天、外壳的脚本；外壳提供：
//   route()（把地址拆成 [页, …]）、render()（按地址重画当前页）、backLink(href, label)（返回上一页的链接）、toolsChanged()（加 / 撤工具与子代理后重画）。
      const $ = (s, r = document) => r.querySelector(s);
      const esc = (s) =>
        String(s ?? "").replace(
          /[&<>"']/g,
          (c) =>
            ({
              "&": "&amp;",
              "<": "&lt;",
              ">": "&gt;",
              '"': "&quot;",
              "'": "&#39;",
            })[c],
        );
      const pad = (n) => String(n).padStart(2, "0");
      let API_GEN = 0;   // 每次改东西的请求（点按钮等）发出前、回来后各加一
      const api = async (p, o) => {
        const w = o?.method && o.method !== "GET";
        if (w) API_GEN++;
        try {
          const r = await fetch(p, o);
          const j = await r.json().catch(() => ({ error: r.statusText }));
          if (!r.ok) throw new Error(j.error || r.statusText);
          return j;
        } finally {
          if (w) API_GEN++;
        }
      };
      // 后台定时刷新用：拉取途中点了按钮，拉到的是点之前的旧状态，返回 null 丢掉，免得把刚点出来的新状态盖回去再跳过来
      const poll = async (p) => {
        const g = API_GEN, j = await api(p);
        return g === API_GEN ? j : null;
      };

      // ---------- 数据 ----------
      const D = {
        state: null,
        tasks: [],
        runs: [],
        tools: [],
        subagents: [],
        avg: {},
        loaded: false,
      };

      // ---------- 字段翻译 ----------
      const STATUS = {
        running: ["运行中", "●"],
        complete: ["完成", "✓"],
        partial: ["部分完成", "◐"],
        paused: ["已暂停", "‖"],
        waiting: ["待处理", "?"],
        stepping: ["等下一步", "…"],
        stopped: ["已停止", "■"],
        resuming: ["排队续跑", "…"],
        failed: ["失败", "✕"],
        timeout: ["超时", "⏱"],
        queued: ["排队中", "…"],
      };
      const stPill = (s) => {
        const [t, i] = STATUS[s] || [s || "未知", "·"];
        return `<span class="st ${esc(s)}" data-ico="${i}">${t}</span>`;
      };
      const TRIG = {
        schedule: "定时",
        dream: "空闲整理",
        manual: "手动",
        file: "目录变更",
        listen: "监听",
        catchup: "补跑",
        timer: "定时",
      };
      const trigName = (t) => TRIG[t] || t || "—";
      const ERR = {
        SCOPE: "调用被拒",
        TOOL_NOT_ALLOWED: "未授权工具",
        LIMIT: "超出上限",
        HOOK_BLOCKED: "hook 拦下",
        CREDENTIAL: "缺凭证",
        BUDGET: "额度耗尽",
        PLATFORM: "平台故障",
        TIMEOUT: "超时",
        READER_FAILED: "子代理出错",
        MODEL_SWITCH: "模型切换失败",
        MODEL_UNAVAILABLE: "模型不可用",
        RATE_LIMITED: "限流",
      };
      const errName = (c) => ERR[c] || c || "失败";

      function dur(sec) {
        if (sec == null || isNaN(sec)) return "—";
        sec = Math.round(sec);
        if (sec < 60) return `${sec} 秒`;
        if (sec < 3600) {
          const m = Math.floor(sec / 60),
            s = sec % 60;
          return s ? `${m} 分 ${s} 秒` : `${m} 分钟`;
        }
        const h = Math.floor(sec / 3600),
          m = Math.round((sec % 3600) / 60);
        return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
      }
      const durShort = (sec) =>
        sec < 60
          ? `${Math.round(sec)} 秒`
          : sec < 3600
            ? `${Math.round(sec / 60)} 分钟`
            : `${(sec / 3600).toFixed(1).replace(/\.0$/, "")} 小时`;
      function spanText(v) {
        if (v == null || v === "") return null;
        if (typeof v === "number") return dur(v);
        const m = String(v)
          .trim()
          .match(/^(\d+(?:\.\d+)?)\s*([smhd]?)$/);
        if (!m) return String(v);
        return dur(+m[1] * { s: 1, m: 60, h: 3600, d: 86400, "": 1 }[m[2]]);
      }
      const WD = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
      const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
      function relDay(d) {
        const t = new Date(),
          y = new Date(t),
          n = new Date(t);
        y.setDate(t.getDate() - 1);
        n.setDate(t.getDate() + 1);
        return dayKey(d) === dayKey(t)
          ? "今天"
          : dayKey(d) === dayKey(y)
            ? "昨天"
            : dayKey(d) === dayKey(n)
              ? "明天"
              : null;
      }
      const dayWord = (d) =>
        relDay(d) ||
        `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${WD[d.getDay()]}`;
      const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      const when = (d) => (d ? `${dayWord(d)} ${hm(d)}` : "—");
      const toDate = (v) =>
        v == null
          ? null
          : typeof v === "number"
            ? new Date(v * 1000)
            : new Date(v);
      function ago(d) {
        if (!d) return "";
        const s = (Date.now() - d.getTime()) / 1000;
        if (s < 60) return "刚刚";
        if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
        if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
        return `${Math.floor(s / 86400)} 天前`;
      }
      function clock(h, m) {
        const p =
          h < 5
            ? "凌晨"
            : h < 9
              ? "早上"
              : h < 12
                ? "上午"
                : h < 13
                  ? "中午"
                  : h < 18
                    ? "下午"
                    : "晚上";
        return `${p} ${h > 12 ? h - 12 : h}:${pad(m)}`;
      }
      const joinAnd = (a) =>
        a.length <= 1
          ? a.join("")
          : a.length === 2
            ? `${a[0]} 和 ${a[1]}`
            : a.join("、");

      // cron（分 时 日 月 周），本机时区
      function cronField(s, lo, hi) {
        const out = new Set();
        for (const part of s.split(",")) {
          const [r, stp] = part.split("/");
          const st = stp ? +stp : 1;
          let a, b;
          if (r === "*") [a, b] = [lo, hi];
          else if (r.includes("-")) [a, b] = r.split("-").map(Number);
          else [a, b] = [+r, stp ? hi : +r];
          for (let i = a; i <= b; i += st) out.add(i);
        }
        return [...out].sort((x, y) => x - y);
      }
      function cron(expr) {
        const f = String(expr || "")
          .trim()
          .split(/\s+/);
        if (f.length !== 5 || f.some((x) => !/^[\d*,\/-]+$/.test(x)))
          return null;
        return {
          raw: f,
          m: cronField(f[0], 0, 59),
          h: cronField(f[1], 0, 23),
          dom: cronField(f[2], 1, 31),
          mon: cronField(f[3], 1, 12),
          dow: [...new Set(cronField(f[4], 0, 7).map((d) => d % 7))].sort(),
          domAny: f[2] === "*",
          monAny: f[3] === "*",
          dowAny: f[4] === "*",
        };
      }
      const schedList = (v) => (v == null ? [] : Array.isArray(v) ? v : [v]);   // schedule：一条 cron 或几条的列表
      function cronHuman(expr) {
        if (Array.isArray(expr)) {                    // 几条：哪几天都一样就把时刻合在一起说，否则一条一句
          const cs = expr.map(cron);
          if (cs.every((c) => c && c.domAny && c.monAny && /^[\d,]+$/.test(c.raw[0] + c.raw[1])) && new Set(cs.map((c) => c.raw[4])).size === 1) {
            const ts = cs.flatMap((c) => c.h.flatMap((h) => c.m.map((m) => [h, m]))).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
            const one = cronHuman(`${ts[0][1]} ${ts[0][0]} * * ${cs[0].raw[4]}`);   // 拿第一个时刻说一遍，去掉时刻只留「哪几天」
            return one.slice(0, one.length - clock(...ts[0]).length) + joinAnd(ts.map(([h, m]) => clock(h, m)));
          }
          return expr.map(cronHuman).join("；");
        }
        const c = cron(expr);
        if (!c) return String(expr || "");
        const [fm, fh] = c.raw;
        let t;
        if (fh === "*" && fm === "*") t = "每分钟";
        else if (fh === "*" && /^\*\/\d+$/.test(fm))
          t = `每 ${fm.slice(2)} 分钟`;
        else if (fh === "*") t = `每小时第 ${c.m.join("、")} 分`;
        else if (/^\*\/\d+$/.test(fh)) t = `每 ${fh.slice(2)} 小时`;
        else if (c.h.length * c.m.length <= 6)
          t = joinAnd(c.h.flatMap((h) => c.m.map((m) => clock(h, m))));
        else t = `一天 ${c.h.length * c.m.length} 次`;
        let d;
        const dw = c.dow.join(",");
        if (c.domAny && c.dowAny) d = "每天";
        else if (c.domAny)
          d =
            dw === "1,2,3,4,5"
              ? "工作日"
              : dw === "0,6"
                ? "周末"
                : "每" + c.dow.map((x) => WD[x]).join("、");
        else if (c.dowAny) d = `每月 ${c.dom.join("、")} 日`;
        else
          d = `每月 ${c.dom.join("、")} 日及每${c.dow.map((x) => WD[x]).join("、")}`;
        if (!c.monAny) d = `${c.mon.join("、")} 月` + d;
        return d + (/^[每一]/.test(t) ? " " : "") + t;
      }
      function cronDay(c, d) {
        if (!c.monAny && !c.mon.includes(d.getMonth() + 1)) return false;
        const dm = c.dom.includes(d.getDate()),
          dw = c.dow.includes(d.getDay());
        if (c.domAny && c.dowAny) return true;
        if (c.domAny) return dw;
        if (c.dowAny) return dm;
        return dm || dw;
      }
      function cronTimes(expr, day) {
        if (Array.isArray(expr)) return expr.flatMap((e) => cronTimes(e, day)).sort((a, b) => a - b);
        const c = cron(expr);
        if (!c || !cronDay(c, day)) return [];
        const out = [];
        for (const h of c.h)
          for (const m of c.m) {
            out.push(
              new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m),
            );
            if (out.length > 288) return out;
          }
        return out;
      }
      function timerNext(t) {
        return t?.next_run ? new Date(t.next_run * 1000) : null;
      }

      const isOn = (t) => t.enabled === true;
      const schedOn = isOn;
      const hasSched = (t) => !!t.schedule;
      function triggerHuman(t) {
        const s = t.spec || {};
        const parts = [];
        if (t.schedule) parts.push(cronHuman(t.schedule));
        if (s.listen) parts.push(`监听 ${s.listen}`);
        return parts.length ? parts.join(" + ") : "手动";
      }
      const shortModel = (m) =>
        String(m || "")
          .split("/")
          .pop();
      const RUNTIME = {
        "vllm-eugr": "vLLM",
        comfyui: "ComfyUI",
        train: "训练独占",
      };
      function priorityHuman(p) {
        if (p >= 10) return "10（最高）";
        if (p >= 9) return `${p}（高）`;
        if (p <= 2) return `${p}（低）`;
        return `${p}（中）`;
      }

      // 每个任务的预期时长：最近 10 次完成运行的平均
      function computeAvg() {
        const by = {};
        for (const r of D.runs) {
          const s = r.seconds ?? r.elapsed_seconds;
          if (r.status !== "complete" || !s) continue;
          (by[r.task] ||= []).push(s);
        }
        D.avg = {};
        for (const [k, v] of Object.entries(by)) {
          const xs = v.slice(0, 10);
          D.avg[k] = {
            mean: xs.reduce((a, b) => a + b, 0) / xs.length,
            n: xs.length,
          };
        }
      }
      const taskByName = (n) => D.tasks.find((t) => t.name === n);
      const runStart = (r) => toDate(r.first_started || r.started);
      const runEnd = (r) =>
        r.status === "running" ? new Date() : toDate(r.ended) || runStart(r);
      const curElapsed = () =>
        D.state?.current
          ? D.state.current.elapsed + (Date.now() / 1000 - D.state.now)
          : 0;
      // 一次运行的累计时长 = 已结束各段（run.json 的 elapsed_seconds）+ 正在跑的这一段；暂停、挂起的时间不算
      const isCurRun = (r) => r?.status === "running" && D.state?.current?.run_id === r.run_id;
      const runTotal = (r) => (isCurRun(r) ? (r.elapsed_seconds || 0) + curElapsed() : (r.seconds ?? r.elapsed_seconds));
      const curTotal = () => {
        const c = D.state?.current;
        const r = c && D.runs.find((x) => x.run_id === c.run_id && x.task === c.task);
        return (r?.elapsed_seconds || 0) + curElapsed();
      };
      const runNote = (r) => [r.label, r.status === "running" ? "" : r.error].filter(Boolean).join("；");

      // ---------- 运行条 ----------
      // 插队：优先级比正在跑的高、正在跑的又能被抢占（在等它到边界停下）；同 runner.presence_text
      const jumpsQueue = (q) => {
        const c = D.state?.current;
        return !!(c && c.preemptible && +(q.priority ?? 5) > +(c.priority ?? 5));
      };
      function pending() {
        const s = D.state || {};
        return [
          ...(s.queued || []).map((q) => ({ task: q.task, why: jumpsQueue(q) ? "插队中" : "排队", prio: q.priority, at: q.enqueued_at,
            note: q.channel ? String(q.steer || q.prompt || "").split("\n")[0].slice(0, 60) : trigName(q.trigger) })),
          ...(s.paused || []).map((q) => ({ task: q.task, why: "被打断", run: q.run_id })),
        ];
      }
      let PEND_OPEN = false;
      function renderNow() {
        const s = D.state;
        if (!s) return;
        $("#svc").innerHTML = Object.entries(s.services || {})
          .map(
            ([k, v]) =>
              `<span title="${esc(k)}：${esc(v)}"><i class="led ${v === "active" ? "" : "off"}"></i>${esc(k)}</span>`,
          )
          .join("");
        const pe = pending();
        if (!pe.length) PEND_OPEN = false;
        // 点开是一张小列表：每项写清是哪个任务、为什么还在等，点进对应的运行 / 任务页
        const pend = pe.length
          ? `<span class="pend-wrap"><button class="pend" data-pendtoggle aria-expanded="${PEND_OPEN}">${pe.length === 1 ? `${esc(pe[0].task)} ${pe[0].why}` : `${pe.length} 项待处理`}</button>${
              PEND_OPEN
                ? `<div class="pend-pop">${pe
                    .map((p) => `<a class="pend-row" href="${p.run ? `#/runs/${esc(p.task)}/${esc(p.run)}` : `#/tasks/${esc(p.task)}`}"><b>${esc(p.task)}</b><span class="st-tag">${esc(p.why)}</span>${
                      p.prio != null ? `<span class="muted">优先级 ${p.prio}${p.at ? `，${esc(when(toDate(p.at)))} 入队` : ""}</span>` : ""
                    }${p.note ? `<span class="pend-note">${esc(p.note)}</span>` : ""}</a>`)
                    .join("")}</div>`
                : ""
            }</span>`
          : "";
        const el = $("#now");
        const c = s.current;
        const side = (s.side || []).map((u) => `<a class="task" href="#/runs/${esc(u.task)}/${esc(u.run_id)}">${esc(u.task)}</a> 第 ${esc(u.step)} 步`).join("、");
        const sideHtml = side ? `<span class="meta">旁路：${side}</span>` : "";   // stbcc-hub 在跑的 workflow 步骤（不占运行器）
        if (c) {
          const avg = D.avg[c.task];
          const pct = avg
            ? Math.min(100, (curTotal() / avg.mean) * 100)
            : null;
          el.className = "now";
          el.innerHTML = `<i class="pulse"></i><span class="lbl lbl-run">运行中</span><a class="task" href="#/runs/${esc(c.task)}/${esc(c.run_id)}">${esc(c.task)}</a>
            <span class="meta"><b data-tick>${dur(curTotal())}</b><span class="meta-rest">${avg ? ` / 通常 ${durShort(avg.mean)}` : ""}，${c.calls} 次调用${c.last_tool ? `，${esc(c.last_tool)}` : ""}</span></span>
            ${pct != null ? `<span class="mini ${pct >= 100 ? "over" : ""}"><i style="width:${pct}%"></i></span>` : ""}
            ${sideHtml}<span class="end">${pend}</span>`;
        } else if (s.loading) {
          const l = s.loading;                                  // 开跑前加载模型：还没进 current，也要写明是哪个任务
          el.className = "now";
          el.innerHTML = `<i class="pulse"></i><span class="lbl">加载模型</span><a class="task" href="#/tasks/${esc(l.task)}">${esc(l.task)}</a>
            <span class="meta"><span class="meta-rest">${l.model ? esc(l.model) + "，" : ""}已 </span><b data-tick>${dur(s.now - l.since)}</b></span>
            ${sideHtml}<span class="end">${pend}</span>`;
        } else {
          const down = s.services && s.services["stbcc-runner"] !== "active";
          const nx = nextScheduled();
          el.className = "now idle";
          el.innerHTML = `<i class="pulse"></i><span class="lbl">${down ? "调度服务已停止" : "空闲"}</span>
            ${nx && !down ? `<span class="meta">下一个 <a class="task" href="#/tasks/${esc(nx.t.name)}">${esc(nx.t.name)}</a>，${when(nx.at)}</span>` : ""}
            ${sideHtml}<span class="end">${pend}</span>`;
        }
      }
      function nextScheduled() {
        let best = null;
        for (const t of D.tasks) {
          if (!hasSched(t) || !schedOn(t)) continue;
          const at = timerNext(t);
          if (at && (!best || at < best.at)) best = { t, at };
        }
        return best;
      }
      setInterval(() => {
        const b = $("[data-tick]");
        if (b && D.state?.current) b.textContent = dur(curTotal());
        else if (b && D.state?.loading) b.textContent = dur(Date.now() / 1000 - D.state.loading.since);   // 开跑前加载模型
        // 运行详情页：正在跑的这次的总耗时跟运行条一起每秒走；跑完了重画一次，换成最终的状态与耗时
        for (const el of document.querySelectorAll("[data-runtotal]")) {
          const c = D.state?.current;
          if (c && `${c.task}/${c.run_id}` === el.dataset.runtotal) el.textContent = dur(+el.dataset.base + curElapsed());
          else if (!el.dataset.done) { el.dataset.done = 1; render(); }
        }
      }, 1000);

      // ---------- 时间轴 ----------
      let TL_DAY = 0; // 相对今天的天数
      function tlBase() {
        const b = new Date();
        b.setHours(0, 0, 0, 0);
        b.setDate(b.getDate() + TL_DAY);
        return b;
      }
      function dayNavHTML() {
        const b = tlBase(),
          r = relDay(b);
        return `<button class="arrow" data-tlstep="-1" aria-label="前一天">‹</button><b>${b.getMonth() + 1} 月 ${b.getDate()} 日 ${WD[b.getDay()]}${r ? `（${r}）` : ""}</b><button class="arrow" data-tlstep="1" aria-label="后一天">›</button>${TL_DAY ? `<button class="btn sm" data-tlstep="0">回到今天</button>` : ""}`;
      }
      function timelineHTML() {
        const base = tlBase();
        const t0 = base.getTime(),
          t1 = t0 + 86400000;
        const X = (d) =>
          Math.max(0, Math.min(100, ((d.getTime() - t0) / 86400000) * 100));
        const lanes = [];
        const scheduled = D.tasks
          .filter(hasSched)
          .sort(
            (a, b) =>
              (cronTimes(a.schedule, base)[0] || 0) -
              (cronTimes(b.schedule, base)[0] || 0),
          );
        const dayRuns = D.runs.filter((r) => {
          const a = runStart(r),
            b = runEnd(r);
          return a && b && b.getTime() >= t0 && a.getTime() < t1;
        });
        const used = new Set();
        const runBars = (rs) =>
          rs
            .map((r) => {
              const segs =
                r.segments && r.segments.length
                  ? r.segments.map((g) => [
                      toDate(g.started),
                      g.ended ? toDate(g.ended) : new Date(),
                    ])
                  : [[runStart(r), runEnd(r)]];
              return (
                segs
                  .map(([a, b]) => {
                    if (b.getTime() < t0 || a.getTime() >= t1) return "";
                    const l = X(a);
                    return `<div class="tl-run ${esc(r.status)}" style="left:${l}%;width:${Math.max(0.2, X(b) - l)}%"></div>`;
                  })
                  .join("") +
                `<a class="tl-hit" href="#/runs/${esc(r.task)}/${esc(r.run_id)}" style="left:${X(segs[0][0])}%;width:${Math.max(1, X(segs[segs.length - 1][1]) - X(segs[0][0]))}%" data-tip="${esc(JSON.stringify({ k: "run", task: r.task, run: r.run_id }))}"></a>`
              );
            })
            .join("");
        for (const t of scheduled) {
          const on = schedOn(t),
            avg = D.avg[t.name];
          const fires = cronTimes(t.schedule, base);
          const rs = dayRuns.filter((r) => r.task === t.name);
          rs.forEach((r) => used.add(r.run_id));
          let h = "";
          if (avg && on)
            for (const f of fires)
              h += `<div class="tl-exp" style="left:${X(f)}%;width:${Math.max(0.3, (avg.mean / 86400) * 100)}%"></div>`;
          h += runBars(rs);
          for (const f of fires) {
            const l = X(f);
            const match = rs.find(
              (r) =>
                Math.abs(runStart(r) - f) < 3 * 3600000 &&
                runStart(r) >= f - 120000,
            );
            h += `<div class="tl-pt ${on ? "" : "off"}" style="left:${l}%"></div>`;
            h += `<div class="tl-cap${l > 12 ? " flip" : ""}" style="left:${l}%">${hm(f)}${on ? (avg ? `  约 ${durShort(avg.mean)}` : "") : "  已禁用"}</div>`;
            h += `<div class="tl-hit" style="left:${l}%" data-tip="${esc(JSON.stringify({ k: "fire", task: t.name, at: f.getTime(), run: match?.run_id || null }))}"></div>`;
          }
          lanes.push(
            `<div class="tl-lane"><div class="tl-name"><a href="#/tasks/${esc(t.name)}"><b>${esc(t.name)}</b></a>${on ? "" : `<span>已禁用</span>`}</div><div class="tl-track">${h}</div></div>`,
          );
        }
        // 当天跑过、但不是定时触发的任务
        const other = {};
        for (const r of dayRuns)
          if (!used.has(r.run_id)) (other[r.task] ||= []).push(r);
        for (const [name, rs] of Object.entries(other)) {
          const t = taskByName(name);
          lanes.push(
            `<div class="tl-lane"><div class="tl-name"><a href="#/tasks/${esc(name)}"><b>${esc(name)}</b></a></div><div class="tl-track">${runBars(rs)}</div></div>`,
          );
        }
        let ticks = "",
          grid = "";
        for (let hr = 0; hr <= 24; hr++) {
          if (hr % 3 === 0 && !(TL_DAY === 0 && Math.abs((hr / 24) * 100 - X(new Date())) < 3))
            ticks += `<span style="left:${(hr / 24) * 100}%">${pad(hr)}:00</span>`;
          grid += `<i class="${hr % 6 === 0 ? "major" : ""}" style="left:${(hr / 24) * 100}%"></i>`;
        }
        const nowL =
          TL_DAY === 0
            ? `<div class="tl-nowline" style="left:calc(${X(new Date())}% - 1px)"></div>`
            : "";
        return `<div class="tl"><div class="tl-grid">${grid}${nowL}</div><div class="tl-axis"><div></div><div class="tl-ticks">${ticks}</div></div>
          ${lanes.join("") || `<div class="empty">当天无定时任务，也无运行记录</div>`}</div>${agendaHTML(t0, scheduled, dayRuns)}`;
      }
      // 手机上的时间轴：24 小时压进一屏宽、每个任务一行时色条细得看不见。改成顶上一条全天色带（任务串行，一条就画得下）+ 按时间排的清单
      function agendaHTML(t0, scheduled, dayRuns) {
        const now = Date.now(),
          base = new Date(t0),
          X = (ms) => Math.max(0, Math.min(100, ((ms - t0) / 86400000) * 100)),
          items = [],
          strip = [];
        for (const r of dayRuns) {
          const segs = r.segments?.length ? r.segments.map((g) => [toDate(g.started).getTime(), g.ended ? toDate(g.ended).getTime() : now]) : [[runStart(r).getTime(), runEnd(r).getTime()]],
            href = `#/runs/${esc(r.task)}/${esc(r.run_id)}`;
          for (const [a, b] of segs) if (b >= t0 && a < t0 + 86400000) strip.push(`<a class="ag-seg ${esc(r.status)}" href="${href}" style="left:${X(a)}%;width:${X(b) - X(a)}%" aria-label="${esc(r.task)}"></a>`);
          items.push({
            at: runStart(r).getTime(),
            html: `<a class="ag-li" href="${href}"><span class="ag-at">${runStart(r).getTime() < t0 ? "<small>前一天</small>" : ""}${hm(runStart(r))}</span><i class="ag-dot ${esc(r.status)}"></i>
              <span class="ag-main"><b>${esc(r.task)}</b><span class="ag-sub"><em class="${esc(r.status)}">${STATUS[r.status]?.[0] || esc(r.status || "未知")}</em>${r.trigger ? `，${esc(trigName(r.trigger))}` : ""}</span></span><span class="ag-d">${dur(runTotal(r))}</span></a>`,
          });
        }
        for (const t of scheduled) {
          const on = schedOn(t),
            avg = D.avg[t.name],
            rs = dayRuns.filter((r) => r.task === t.name);
          for (const f of cronTimes(t.schedule, base)) {
            if (rs.some((r) => Math.abs(runStart(r) - f) < 3 * 3600000 && runStart(r) >= f - 120000)) continue;   // 已有对应运行：清单里那条运行就是它
            const later = f.getTime() > now;
            if (on && later) strip.push(`${avg ? `<i class="ag-exp" style="left:${X(f.getTime())}%;width:${X(f.getTime() + avg.mean * 1000) - X(f.getTime())}%"></i>` : ""}<i class="ag-fire" style="left:${X(f.getTime())}%"></i>`);   // 待触发：浅灰块是预计时长
            items.push({
              at: f.getTime(),
              html: `<a class="ag-li fire" href="#/tasks/${esc(t.name)}"><span class="ag-at">${hm(f)}</span><i class="ag-dot"></i>
                <span class="ag-main"><b>${esc(t.name)}</b><span class="ag-sub">${!on ? "已禁用" : later ? "待触发" : "无对应运行记录"}</span></span><span class="ag-d">${on && avg ? `约 ${durShort(avg.mean)}` : ""}</span></a>`,
            });
          }
        }
        if (!items.length) return `<div class="tl-list"><div class="empty">当天无定时任务，也无运行记录</div></div>`;
        items.sort((a, b) => a.at - b.at);
        if (TL_DAY === 0) {
          const k = items.findIndex((x) => x.at > now);
          items.splice(k < 0 ? items.length : k, 0, { now: true, html: `<div class="ag-now"><span>现在 ${hm(new Date())}</span></div>` });
        }
        // 每页 7 个节点；分页以正在运行的那个（没有就是「现在」那条线前一个）为界，默认那页它落在最下面
        const nodes = items.filter((x) => !x.now),
          N = nodes.length,
          run = nodes.findIndex((x) => x.html.includes("ag-dot running")),
          nowAt = items.findIndex((x) => x.now),
          before = nowAt < 0 ? -1 : items.slice(0, nowAt).filter((x) => !x.now).length - 1;
        const e = run >= 0 ? run : before >= 0 ? before : TL_DAY > 0 ? Math.min(6, N - 1) : N - 1,
          pages = [];
        for (let a = e - 6; a > -7; a -= 7) pages.unshift([Math.max(0, a), a + 6]);
        for (let a = e + 1; a < N; a += 7) pages.push([a, Math.min(N - 1, a + 6)]);
        const def = pages.findIndex(([, b]) => b === e);
        if (AG.day !== TL_DAY || AG.page == null || AG.page >= pages.length) { AG.day = TL_DAY; AG.page = def; }
        const [pa, pb] = pages[AG.page];
        let k = -1;
        const shown = items.filter((x) => (x.now ? k >= pa - (pa ? 0 : 1) && k <= pb : (++k, k >= pa && k <= pb)));   // 「现在」线跟着它前一个节点那页
        const pager = pages.length > 1
          ? `<div class="ag-pager"><button class="arrow" data-agpage="-1" ${AG.page ? "" : "disabled"} aria-label="更早">‹</button><span><b>第 ${AG.page + 1} / ${pages.length} 页</b><small>${nodes[pa].at < t0 ? "前一天 " : ""}${hm(new Date(nodes[pa].at))}–${hm(new Date(nodes[pb].at))}</small></span><button class="arrow" data-agpage="1" ${AG.page < pages.length - 1 ? "" : "disabled"} aria-label="更晚">›</button>${AG.page !== def ? `<button class="btn sm" data-agpage="0">${TL_DAY === 0 ? "回到现在" : TL_DAY > 0 ? "回到开头" : "回到最后"}</button>` : ""}</div>`
          : "";
        return `<div class="tl-list"><div class="ag-strip">${strip.join("")}${TL_DAY === 0 ? `<i class="ag-nowmark" style="left:${X(now)}%"></i>` : ""}</div>
          <div class="ag-hours">${[0, 6, 12, 18, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${pad(h)}:00</span>`).join("")}</div>
          <div class="ag-list">${shown.map((x) => x.html).join("")}</div>${pager}</div>`;
      }
      const AG = { day: null, page: null };   // 手机清单翻到第几页（自动刷新重画时不变，换一天回到默认页）
      function tipHTML(d) {
        const t = taskByName(d.task),
          avg = D.avg[d.task];
        if (d.k === "fire") {
          const at = new Date(d.at);
          const r =
            d.run &&
            D.runs.find((x) => x.run_id === d.run && x.task === d.task);
          return `<b>${esc(d.task)}</b><div class="dim">${esc(t?.description || "")}</div>
            <div>触发：${when(at)}${t && !schedOn(t) ? "（已禁用）" : ""}</div>
            <div>预计：${avg ? `${durShort(avg.mean)}（近 ${avg.n} 次平均）` : "暂无数据"}</div>
            ${r ? `<div>实际：${STATUS[r.status]?.[0] || r.status}，${dur(runTotal(r))}</div>` : at < new Date() && t && schedOn(t) ? `<div class="dim">无对应运行记录</div>` : ""}`;
        }
        const r = D.runs.find((x) => x.run_id === d.run && x.task === d.task);
        if (!r) return "";
        return `<b>${esc(d.task)}</b><div class="dim">${trigName(r.trigger)}，${when(runStart(r))} 开始</div>
          <div>${STATUS[r.status]?.[0] || r.status}，${isCurRun(r) ? `累计 ${dur(runTotal(r))}（本段 ${dur(curElapsed())}）` : dur(runTotal(r))}${r.calls ? `，${r.calls} 次调用` : ""}</div>
          ${runNote(r) ? `<div class="dim">${esc(String(runNote(r)).slice(0, 120))}</div>` : ""}`;
      }
      // 手机没有悬停：点一下显示说明，点别处收起
      document.addEventListener("pointerdown", (e) => { if (e.pointerType !== "mouse") showTip(e); });
      document.addEventListener("mousemove", showTip);
      function showTip(e) {
        const tip = $("#tip");
        const h = e.target.closest?.("[data-tip]");
        if (!h) {
          tip.style.opacity = 0;
          return;
        }
        const html = tipHTML(JSON.parse(h.dataset.tip));
        if (!html) return;
        if (tip.dataset.k !== h.dataset.tip) {
          tip.innerHTML = html;
          tip.dataset.k = h.dataset.tip;
        }
        const w = tip.offsetWidth,
          hh = tip.offsetHeight;
        let x = e.clientX + 14,
          y = e.clientY + 16;
        if (x + w > innerWidth - 8) x = e.clientX - w - 14;
        if (y + hh > innerHeight - 8) y = e.clientY - hh - 12;
        x = Math.max(8, Math.min(x, innerWidth - w - 8));   // 手机屏窄：左右都放不下时夹在屏幕里，不出屏
        y = Math.max(8, Math.min(y, innerHeight - hh - 8));
        tip.style.left = x + "px";
        tip.style.top = y + "px";
        tip.style.opacity = 1;
      }

      // 整台机器在运行期间的 token 用量（含工具内部直接调模型、子代理）；旧运行只有主代理的
      function runTok(r) {
        return r.tokens_all && r.tokens_all.input >= (r.tokens?.input || 0) ? r.tokens_all : r.tokens;
      }

      // ---------- 任务页 ----------
      const lastRun = (name) => D.runs.find((r) => r.task === name);
      function taskRow(t) {
        const lr =
          lastRun(t.name) || (t.last_run && { ...t.last_run, task: t.name });
        const avg = D.avg[t.name];
        const nx = hasSched(t) && schedOn(t) ? timerNext(t) : null;
        const running = D.state?.current?.task === t.name;
        return `<div class="row" data-href="#/tasks/${esc(t.name)}">
          <div><div class="t1"><i class="dot ${isOn(t) ? "" : "off"}" title="${isOn(t) ? "已启用" : "已禁用"}"></i>${esc(t.name)}${t.priority != null ? `<span class="prio" title="1–10，大的先跑">（优先级 ${t.priority}）</span>` : ""}${t.valid ? "" : ` <span class="st failed" data-ico="!">定义有误</span>`}</div>
            <div class="t2">${esc(t.description || t.error || "")}</div></div>
          <div class="kv">${esc(triggerHuman(t))}<span class="muted">${nx ? `下次 ${when(nx)}` : isOn(t) ? "" : "已禁用"}</span></div>
          <div class="kv">${running ? stPill("running") : lr ? `${stPill(lr.status)} <span class="muted">${ago(toDate(lr.ended || lr.started))}${avg ? `，平均 ${durShort(avg.mean)}` : ""}</span>` : `<span class="muted">暂无运行</span>`}</div>
          <div class="menu-wrap">${t.valid ? `<button class="kebab" data-menu aria-label="更多操作" aria-haspopup="menu">⋯</button>
            <div class="menu" role="menu" hidden><button role="menuitem" data-act="run" data-task="${esc(t.name)}">运行</button><button role="menuitem" data-prio="${esc(t.name)}" data-cur="${t.priority ?? 5}" data-max="10">更改优先级</button>${hasSched(t) ? `<button role="menuitem" data-schededit="${esc(t.name)}">更改定时</button>` : ""}${isOn(t) ? `<button role="menuitem" class="bad" data-act="disable" data-task="${esc(t.name)}">禁用</button>` : `<button role="menuitem" data-act="enable" data-task="${esc(t.name)}">启用</button>`}</div>` : ""}</div>
        </div>`;
      }
      function viewTasks() {
        const groups = [
          ["定时任务", "按计划自动执行", D.tasks.filter(hasSched)],
          [
            "监控任务",
            "由监控触发执行",
            D.tasks.filter((t) => !hasSched(t) && t.spec?.listen),
          ],
          [
            "手动任务",
            "仅手动执行",
            D.tasks.filter((t) => !hasSched(t) && !t.spec?.listen),
          ],
        ].filter((g) => g[2].length);
        return `<h1>任务</h1><p class="lead">${D.tasks.length} 个任务，${D.tasks.filter(isOn).length} 个启用。任务串行执行，按优先级排队。</p>
          <section class="card">
            <div class="card-head"><h2>时间轴</h2><div class="daynav" id="daynav">${dayNavHTML()}</div>
              <div class="legend spacer">
                <span><i class="sw" style="width:11px;height:11px;border-radius:50%;border:2.5px solid var(--ink)"></i>触发</span>
                <span><i class="sw" style="width:22px;height:10px;background:var(--expected)"></i>预计时长</span>
                <span><i class="sw" style="width:22px;height:5px;background:var(--ok)"></i>实际运行（颜色 = 结果）</span>
              </div></div>
            <div id="tl">${timelineHTML()}</div>
          </section>
          ${groups.map(([h, s, ts]) => `<section class="group"><div class="group-head"><h2>${h}</h2><span>${s}</span></div><div class="rows">${ts.map(taskRow).join("")}</div></section>`).join("")}`;
      }

      function optRows(extra) {
        const rows = extra || [];
        return rows.length
          ? `<div class="scope">${rows.map(([a, b]) => `<div class="k">${esc(a)}</div><div>${esc(b)}</div>`).join("")}</div>`
          : "";
      }

      function viewTask(name) {
        const back = backLink("#/tasks", "全部任务");
        const t = taskByName(name);
        if (!t)
          return `${back}<h1>${esc(name)}</h1><div class="alert bad">任务不存在。</div>`;
        if (!t.valid)
          return `${back}<h1>${esc(name)}</h1><div class="alert bad">定义有误，平台不会执行：${esc(t.error)}</div><div class="card"><pre class="code">${esc(t.yaml)}</pre></div>`;
        const s = t.spec || {};
        const avg = D.avg[t.name];
        const nx = hasSched(t) && schedOn(t) ? timerNext(t) : null;
        const onBtn = isOn(t)
          ? `<button class="btn" data-act="disable" data-task="${esc(t.name)}" data-confirm="确认禁用">禁用</button>`
          : `<button class="btn" data-act="enable" data-task="${esc(t.name)}">启用</button>`;
        const isCur = D.state?.current?.task === t.name;
        const paused = lastRun(t.name)?.status === "paused";
        const trig = [
          [
            "状态",
            `${isOn(t) ? `<span class="st on" data-ico="●">已启用</span>` : `<span class="st off" data-ico="○">已禁用</span>`}`,
          ],
          [
            "触发",
            `${esc(triggerHuman(t))}${t.schedule ? `<span class="sub mono">${esc(schedList(t.schedule).join("  ;  "))}</span>` : ""}${hasSched(t) ? ` <button class="btn sm" data-schededit="${esc(t.name)}">修改</button>` : ""}`,
          ],
          nx ? ["下次运行", when(nx)] : null,
          s.listen
            ? [
                "监听",
                `<span class="mono">tasks/${esc(t.name)}/listen.py</span>` +
                  [
                    s.listen_events?.length ? `事件 ${esc(s.listen_events.join("、"))}` : "",
                  ]
                    .filter(Boolean)
                    .map((x) => `<span class="sub">${x}</span>`)
                    .join(""),
              ]
            : null,
          ["优先级", esc(priorityHuman(t.priority))],
          ["重叠策略", t.overlap === "queue" ? "排队（订阅了事件）" : "跳过"],
        ].filter(Boolean);
        if (Array.isArray(s.steps)) return viewWorkflow(t, back, onBtn, trig, avg);   // workflow 任务：只有步骤，单独的页面
        const env = [
          ["模型", `${esc(shortModel(t.model))}<span class="sub">${esc(RUNTIME[t.runtime] || t.runtime)}</span>`],
          ["工作方式", "Agent（沙箱 + 工具网关）"],
          ["沙箱", `一次性容器<span class="sub">${esc(t.sandbox.image)}，只连工具网关，内存 ${esc(t.sandbox.memory)}，工作目录持久</span>`],
          s.memory ? ["记忆", "注入 USER.md、memory.md"] : null,
        ].filter(Boolean);
        const lim = [
          ["模型时间", `${esc(spanText(t.timeout))}<span class="sub">仅计主模型工作时间，耗尽后收尾</span>`],
          t.stall_seconds
            ? ["停滞判定", `${esc(spanText(t.stall_seconds))}无输出且没有在途工具调用`]
            : null,
          [
            "可抢占",
            t.preemptible ? `是，等检查点最多 ${esc(spanText(t.preempt_grace_seconds))}` : "否",
          ],
          ["成功通知", "私信（agent 自己发过消息就不补发）"],
          ["失败通知", "私信错误摘要"],
          avg
            ? [
                "平均耗时",
                `${durShort(avg.mean)}<span class="sub">近 ${avg.n} 次完成</span>`,
              ]
            : null,
        ].filter(Boolean);
        const dlh = (rows) =>
          `<dl>${rows.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>`;
        const toolRow = (u) => {
            const spec = D.tools.find((x) => x.name === u.name) || {};
            const extra = [];
            if (u.guide) extra.push(["本任务的用法规则", u.guide]);
            return `<div class="tool"><div class="tool-head"><a href="#/tools/${esc(u.name)}"><b>${esc(u.name)}</b></a>${platTag(spec)}
              <button class="btn sm spacer" data-toolrm="${esc(u.name)}" data-task="${esc(t.name)}">撤销</button></div>
            ${spec.description ? `<p>${esc(spec.description)}</p>` : ""}${optRows(extra)}</div>`;
          };
        const subRow = (u) => {                                         // 任务 yaml 的 subagents：主模型直接调用的子代理（撤销 / 添加和工具一样）
            const a = D.subagents.find((x) => x.name === u.name) || {};
            const what = a.kind === "agent" ? (a.tools || []).map((x) => x.name) : (a.inputs || []).map((x) => x.name);
            return `<div class="tool"><div class="tool-head"><a href="#/subagents/${esc(u.name)}"><b>${esc(u.name)}</b></a><span class="tag acc">子代理</span>${a.kind ? `<span class="tag">${esc(a.kind)}</span>` : ""}${a.isolated ? `<span class="tag">隔离</span>` : ""}
              <button class="btn sm spacer" data-toolrm="${esc(u.name)}" data-kind="subagents" data-task="${esc(t.name)}">撤销</button></div>
            ${a.description ? `<p>${esc(a.description)}</p>` : ""}${what.length ? `<div class="small muted" style="margin-top:6px">${a.kind === "agent" ? "工具" : "参数"}：${what.map((n) => `<span class="mono">${esc(n)}</span>`).join("、")}</div>` : ""}${optRows(u.guide ? [["本任务的用法规则", u.guide]] : [])}</div>`;
          };
        const plainT = t.tools || [], subs = s.subagents || [];          // 任务的 tools 只能是 universal 的工具
        const tools = (plainT.length
          ? `<h3 class="tgroup">工具<span>${plainT.length} 个</span></h3>${plainT.map(toolRow).join("")}`
          : "") + (subs.length ? `<h3 class="tgroup">子代理<span>${subs.length} 个</span></h3>${subs.map(subRow).join("")}` : "");
        return `${back}
          <div class="title-row"><h1>${esc(t.name)}</h1>${isCur ? stPill("running") : ""}
            <div class="acts">${paused ? `<button class="btn" data-act="resume" data-task="${esc(t.name)}">恢复</button>` : ""}${onBtn}<button class="btn primary" data-act="run" data-task="${esc(t.name)}">运行</button></div></div>
          <p class="lead">${esc(t.description || "")}</p>
          <div class="grid">
            <section class="card"><h3>触发</h3>${dlh(trig)}</section>
            <section class="card"><h3>运行环境</h3>${dlh(env)}</section>
            <section class="card"><h3>限额与通知</h3>${dlh(lim)}</section>
          </div>
          ${`<section class="card"><div class="card-head"><h2>授权工具</h2><span class="hint">未列出的工具调用会被拒绝${isCur ? "；运行中撤销即时生效，添加从下次开跑生效" : ""}</span>
            <button class="btn sm spacer" data-addopen>添加工具</button><button class="btn sm" data-addopen="subagents">添加子代理</button></div>
            <div id="tool-add" hidden>${addPanel(t)}</div>${tools || `<div class="empty">还没有授权工具</div>`}</section>`}
          <section class="card"><div class="card-head"><h2>运行记录</h2></div><div id="task-runs">${runsTable(
            D.runs.filter((r) => r.task === t.name),
            false,
            "task",
          )}</div></section>
          <section class="card">
            ${t.system_prompt ? `<details><summary>系统提示词<span class="muted">${t.system_prompt.length} 字</span></summary><div class="prose">${esc(t.system_prompt)}</div></details>` : ""}
            <details><summary>原始 yaml<span class="muted">其余字段修改请走 git</span></summary><pre class="code">${esc(t.yaml)}</pre></details>
          </section>`;
      }

      // ---------- workflow 任务 ----------
      // workflow 任务只有步骤（task yaml 的 steps：[编号, 文件, 要不要资源, {runtime, model}]），没有工具、子代理、模型；任务页和运行页都以步骤为主
      const wfSteps = (raw) => (raw || []).map((x) => (Array.isArray(x) ? { n: x[0], file: x[1], needs: !!x[2], model: x[3]?.model || null } : x));
      const wfSegs = (r, n) => (r.segments || []).filter((g) => (g.step ?? 1) === n);   // 早期记录的段不带 step，算第 1 步
      const segSecs = (g) => (g.ended ? g.seconds ?? g.ended - g.started : Date.now() / 1000 - g.started);
      const wfSecs = (r, n) => wfSegs(r, n).reduce((a, g) => a + segSecs(g), 0);
      // 一次运行里第 n 步的状态：看这一步最后一段（没结束 = 正在跑）；没有段的，运行已完成算没跑（前面的步骤说没活了），否则未开始
      function wfState(r, n) {
        const gs = wfSegs(r, n);
        if (!gs.length) return r.status === "complete" ? "skip" : "todo";
        const g = gs[gs.length - 1];
        if (!g.ended) return "running";
        return ["complete", "stepping"].includes(g.status) || !g.status ? "done" : g.status;
      }
      const WF_ST = {
        done: ["完成", "✓", "complete"], running: ["运行中", "●", "running"], paused: ["已暂停", "‖", "paused"], failed: ["失败", "✕", "failed"],
        timeout: ["超时", "⏱", "timeout"], stopped: ["已停止", "■", "off"], skip: ["没跑", "–", "off"], todo: ["未开始", "○", "off"],
      };
      const wfPill = (st) => {
        const [l, i, c] = WF_ST[st] || [st, "", "off"];
        return `<span class="st ${c}" data-ico="${i}">${esc(l)}</span>`;
      };
      // 改成 workflow 之前的命令任务的运行：段和运行都不带 step
      const wfLegacy = (r) => r.step == null && !(r.segments || []).some((g) => g.step != null);
      const wfUnfinished = (r) => r && !["complete", "failed", "timeout", "stopped"].includes(r.status);
      // 第 n 步最近 10 次完成运行的平均时长
      function wfAvg(name, n) {
        const xs = D.runs
          .filter((r) => r.task === name && r.status === "complete" && !wfLegacy(r))
          .map((r) => wfSegs(r, n))
          .filter((gs) => gs.length && gs.every((g) => g.ended))
          .slice(0, 10)
          .map((gs) => gs.reduce((a, g) => a + segSecs(g), 0));
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
      }
      const wfNeeds = (s) => (s.needs ? `占机器${s.model ? `，模型 ${esc(shortModel(s.model))}` : "，不起模型（只空出机器）"}` : "不占资源，旁路跑：不排队、不打断别的任务");
      // 步骤条：一步一行，编号圆点用竖线连着；r 给了就标这次运行里每一步的状态，more(s) 给每一步追加内容
      function wfStepper(steps, r, time, more) {
        return `<ol class="wf">${steps
          .map((s) => {
            const st = r ? wfState(r, s.n) : "";
            return `<li class="wf-step ${st}"><div class="wf-dot">${st === "done" ? "✓" : esc(s.n)}</div><div class="wf-body">
              <div class="wf-head"><b class="mono">${esc(s.file)}</b>${st ? wfPill(st) : ""}<span class="wf-time">${time(s) || ""}</span></div>
              <div class="wf-meta">${wfNeeds(s)}</div>${more ? more(s, st) : ""}</div></li>`;
          })
          .join("")}</ol>`;
      }
      // 运行记录表的步骤列：每一步一个小点，颜色是这一步的状态
      const wfDots = (r, steps) => wfLegacy(r) ? `<span class="muted small">旧版命令任务</span>` :
        `<span class="wf-dots">${steps.map((s) => `<i class="${WF_ST[wfState(r, s.n)]?.[2] || "off"}" title="第 ${esc(s.n)} 步 ${esc(s.file)}：${esc(WF_ST[wfState(r, s.n)]?.[0] || "")}"></i>`).join("")}</span>`;

      function viewWorkflow(t, back, onBtn, trig, avg) {
        const s = t.spec || {};
        const steps = wfSteps(s.steps);
        const last = lastRun(t.name);
        const live = wfUnfinished(last) ? last : null;          // 没跑完的那次：步骤条按它标状态
        const isCur = live?.status === "running";
        const time = (x) => {
          if (live && ["running", "paused", "done"].includes(wfState(live, x.n))) return dur(wfSecs(live, x.n));
          const a = wfAvg(t.name, x.n);
          return a != null ? `平均 ${durShort(a)}` : "";
        };
        const rules = [
          ["运行上限", `${esc(spanText(t.timeout))}<span class="sub">各步骤在跑的时间合计，到点终止记超时</span>`],
          ["可抢占", t.preemptible ? `是<span class="sub">占机器的步骤中间可被更高优先级的任务打断，恢复时重跑这一步；步骤之间也会让出</span>` : `否<span class="sub">占机器的步骤跑完才让出</span>`],
          ["联网", s.network === true ? "可以" : Array.isArray(s.network) ? `只连本机端口 ${s.network.map((p) => `<span class="mono">${esc(p)}</span>`).join("、")}` : "不可以"],
          ["成功通知", "私信一条（步骤写的 reply.txt，没写就发完成）"],
          ["失败通知", "私信错误摘要"],
          avg ? ["平均耗时", `${durShort(avg.mean)}<span class="sub">近 ${avg.n} 次完成</span>`] : null,
        ].filter(Boolean);
        const dlh = (rows) => `<dl>${rows.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>`;
        const paused = last?.status === "paused";
        return `${back}
          <div class="title-row"><h1>${esc(t.name)}</h1><span class="tag">workflow</span>${live ? stPill(live.status) : ""}
            <div class="acts">${paused ? `<button class="btn" data-act="resume" data-task="${esc(t.name)}">恢复</button>` : ""}${onBtn}<button class="btn primary" data-act="run" data-task="${esc(t.name)}">运行</button></div></div>
          <p class="lead">${esc(t.description || "")}</p>
          <section class="card"><div class="card-head"><h2>步骤</h2><span class="hint">${steps.length} 步，按顺序跑任务文件夹 tasks/${esc(t.name)}/ 里的脚本</span>
            ${live ? `<a class="spacer small" href="#/runs/${esc(t.name)}/${esc(live.run_id)}">这次运行的详情 ›</a>` : ""}</div>
            ${wfStepper(steps, live, time)}</section>
          <div class="grid">
            <section class="card"><h3>触发</h3>${dlh(trig)}</section>
            <section class="card"><h3>运行规则</h3>${dlh(rules)}</section>
          </div>
          <section class="card"><div class="card-head"><h2>运行记录</h2></div><div id="task-runs">${runsTable(D.runs.filter((r) => r.task === t.name), false, "task", steps)}</div></section>
          <section class="card"><details><summary>原始 yaml<span class="muted">修改请走 git</span></summary><pre class="code">${esc(t.yaml)}</pre></details></section>`;
      }

      // workflow 的一次运行：每一步的状态、时长、分了几段、输出（step-<编号>.log 的末尾），以及发给 Joy 的 reply.txt
      function wfRunHTML(task, d) {
        const r = d.run, w = d.workflow;
        const st = toDate(r.first_started || r.started), en = toDate(r.ended);
        const stat = (a, b) => `<div class="stat"><span>${a}</span><b>${b}</b></div>`;
        const done = w.steps.filter((s) => wfState(r, s.n) === "done").length;
        const total = (r.segments || []).reduce((a, g) => a + segSecs(g), 0);
        const kb = (n) => (n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} 字节`);
        const more = (s, state) => {
          const gs = wfSegs(r, s.n);
          const segs = gs.length > 1 ? `<div class="wf-meta">分 ${gs.length} 段跑完（中间被抢占或暂停 ${gs.length - 1} 次）</div>` : "";
          const lg = s.log || {};
          return `${segs}${lg.len ? `<details class="wf-log" ${["running", "failed", "timeout"].includes(state) ? "open" : ""}><summary>输出<span class="muted">${s.file === "command" ? "command.log" : `step-${esc(s.n)}.log`}，${kb(lg.len)}${lg.cut ? "，只显示末尾" : ""}</span></summary><pre class="code">${esc(lg.text)}</pre></details>` : ""}`;
        };
        return `${backLink(`#/tasks/${task}`, task)}
          <div class="title-row"><h1>${esc(task)}</h1><span class="tag">workflow</span>${stPill(r.status)}</div>
          <p class="lead">${trigName(r.trigger)}触发，${when(st)} 开始${en && r.status !== "running" ? `，${when(en)} 结束` : ""}<br><span class="mono">${esc(d.dir)}</span></p>
          ${runNote(r) ? `<div class="alert ${["failed", "timeout"].includes(r.status) ? "bad" : "warn"}">${esc(runNote(r))}</div>` : ""}
          <section class="card"><div class="stats">
            ${stat("总耗时", dur(total || runTotal(r)))}
            ${stat("步骤", `${done} / ${w.steps.length} 完成`)}
            ${r.paused_seconds ? stat("暂停", dur(r.paused_seconds)) : ""}
            ${r.preempt_count ? stat("被抢占", `${r.preempt_count} 次`) : ""}
            ${r.exit_code != null && r.exit_code !== 0 ? stat("退出码", esc(r.exit_code)) : ""}
            ${r.memory_peak ? stat("内存峰值", `${r.memory_peak.mem_used_gb} GB`) : ""}
          </div></section>
          <section class="card"><div class="card-head"><h2>步骤</h2></div>${wfStepper(w.steps, r, (s) => (wfSegs(r, s.n).length ? dur(wfSecs(r, s.n)) : ""), more)}</section>
          ${w.reply ? `<section class="card"><h3>发给 Joy 的消息<span class="muted small" style="margin-left:8px">steps/reply.txt</span></h3><div class="prose" style="margin:0">${esc(w.reply)}</div></section>`
            : r.final_text ? `<section class="card"><h3>结果</h3><div class="prose" style="margin:0">${esc(r.final_text)}</div></section>` : ""}
          ${d.out_files.length ? `<section class="card"><h3>产出文件（工作目录 out/）</h3><div class="taglist">${d.out_files.map((f) => `<span class="tag mono">${esc(f)}</span>`).join("")}</div></section>` : ""}
          <section class="card"><details><summary>任务快照<span class="muted">这次运行开始时的 task.yaml</span></summary><pre class="code">${esc(d.task_yaml)}</pre></details></section>`;
      }

      // ---------- 修改定时 ----------
      // schedule 是一条 cron 或几条的列表（几个时刻分钟不一样时）。编辑器：频率（每天 / 每周几 / 每月几号）+ 哪几个月 + 几个时刻；
      // 每周和每月二选一（cron 的日与周同时写是「或」，不好懂）。保存走 /api/tasks/<名>/schedule（save_task 完整校验）
      const SCHED = { task: "", freq: "day", dow: new Set(), dom: new Set(), mon: new Set(), times: [], odd: "" };
      const MONTHS = ["1 月", "2 月", "3 月", "4 月", "5 月", "6 月", "7 月", "8 月", "9 月", "10 月", "11 月", "12 月"];
      // 数字集合 → cron 字段：连着的写成 a-b
      function cronList(set) {
        const xs = [...set].sort((x, y) => x - y), out = [];
        for (let i = 0; i < xs.length; i++) {
          let j = i;
          while (j + 1 < xs.length && xs[j + 1] === xs[j] + 1) j++;
          out.push(j - i >= 2 ? `${xs[i]}-${xs[j]}` : xs.slice(i, j + 1).join(","));
          i = j;
        }
        return out.join(",");
      }
      // 现在的 schedule 能不能用编辑器表示：分、时是数字；各条的日、月、周一样；日和周不同时写
      function schedParse(v) {
        const cs = schedList(v).map((e) => String(e).trim().split(/\s+/));
        if (!cs.length || cs.some((f) => f.length !== 5 || !/^\d+(,\d+)*$/.test(f[0]) || !/^\d+(,\d+)*$/.test(f[1]) || !cron(f.join(" ")))) return null;
        if (new Set(cs.map((f) => f.slice(2).join(" "))).size !== 1) return null;
        const [, , dm, mo, dw] = cs[0];
        if (dm !== "*" && dw !== "*") return null;
        const times = [...new Set(cs.flatMap((f) => f[1].split(",").flatMap((h) => f[0].split(",").map((m) => `${pad(+h)}:${pad(+m)}`))))].sort();
        return {
          freq: dw !== "*" ? "week" : dm !== "*" ? "month" : "day",
          dow: new Set(dw === "*" ? [] : cronField(dw, 0, 7).map((d) => d % 7)),
          dom: new Set(dm === "*" ? [] : cronField(dm, 1, 31)),
          mon: new Set(mo === "*" ? [] : cronField(mo, 1, 12)),
          times,
        };
      }
      // 编辑器 → schedule：分钟都一样写一条（如 "0 7,18 * * *"），不一样按分钟分组写成列表
      function schedBuild() {
        if (!SCHED.times.length || (SCHED.freq === "week" && !SCHED.dow.size) || (SCHED.freq === "month" && !SCHED.dom.size)) return null;
        const dm = SCHED.freq === "month" ? cronList(SCHED.dom) : "*";
        const dw = SCHED.freq === "week" ? cronList(SCHED.dow) : "*";
        const mo = SCHED.mon.size && SCHED.mon.size < 12 ? cronList(SCHED.mon) : "*";
        const byMin = {};
        for (const t of [...new Set(SCHED.times)].sort()) {
          const [h, m] = t.split(":").map(Number);
          (byMin[m] ||= []).push(h);
        }
        const xs = Object.entries(byMin).map(([m, hs]) => `${m} ${hs.join(",")} ${dm} ${mo} ${dw}`);
        return xs.length === 1 ? xs[0] : xs;
      }
      // 接下来的几次（本机时区，同时间轴）
      function schedNext(v, n) {
        const out = [], now = new Date();
        for (let i = 0; i < 400 && out.length < n; i++) {
          const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
          out.push(...cronTimes(v, day).filter((d) => d > now));
        }
        return out.slice(0, n);
      }
      // 保存后会不会马上补跑：新 schedule 在现在之前最近的一次（两天内）已过 2 分钟以上、晚于这个任务最近一次成功开跑
      function schedCatchup(name, v) {
        const now = new Date();
        const past = [0, 1].flatMap((i) => cronTimes(v, new Date(now.getFullYear(), now.getMonth(), now.getDate() - i))).filter((d) => now - d > 120000);
        const last = past.sort((a, b) => b - a)[0];
        const ok = D.runs.find((r) => r.task === name && r.status === "complete");
        return last && (!ok || runStart(ok) < last) ? last : null;
      }
      function schedHTML() {
        const t = taskByName(SCHED.task);
        if (!t) return "";
        const v = schedBuild();
        const next = v ? schedNext(v, 3) : [];
        const cu = v ? schedCatchup(t.name, v) : null;
        const changed = v && JSON.stringify(v) !== JSON.stringify(t.schedule);
        const seg = (k, l) => `<button class="${SCHED.freq === k ? "on" : ""}" data-schedfreq="${k}">${l}</button>`;
        const chips = (key, xs, label, cls) => `<div class="sched-chips ${cls}">${xs.map((x) => `<button class="${SCHED[key].has(x) ? "on" : ""}" data-schedpick="${key}:${x}">${label(x)}</button>`).join("")}</div>`;
        const allMon = !SCHED.mon.size || SCHED.mon.size === 12;
        return `<div class="sched-card" role="dialog" aria-label="修改定时">
          <div class="sched-head"><h2>修改定时</h2><span class="muted">${esc(t.name)}</span><button class="iconx" data-schedclose aria-label="关闭">×</button></div>
          <div class="sched-body">
            ${SCHED.odd ? `<div class="alert warn" style="margin:8px 0 0">现在的规则「${esc(SCHED.odd)}」这里改不了；保存会换成下面选的。</div>` : ""}
            <div class="sched-l">频率</div>
            <div class="seg">${seg("day", "每天")}${seg("week", "每周")}${seg("month", "每月")}</div>
            ${SCHED.freq === "week" ? `<div class="sched-l">星期几<span class="sched-quick"><button data-schedset="dow:1,2,3,4,5">工作日</button><button data-schedset="dow:0,6">周末</button></span></div>${chips("dow", [1, 2, 3, 4, 5, 6, 0], (d) => WD[d].slice(1), "round")}` : ""}
            ${SCHED.freq === "month" ? `<div class="sched-l">几号<span class="sched-quick"><button data-schedset="dom:1">1 号</button><button data-schedset="dom:1,15">1 号和 15 号</button></span></div>${chips("dom", Array.from({ length: 31 }, (_, i) => i + 1), String, "grid")}<div class="muted small" style="margin-top:6px">没有这一天的月份（如 2 月 30 日）当月不跑。</div>` : ""}
            <div class="sched-l">哪几个月<span class="sched-quick"><button class="${allMon ? "on" : ""}" data-schedset="mon:">全年</button></span></div>
            ${chips("mon", Array.from({ length: 12 }, (_, i) => i + 1), (m) => MONTHS[m - 1], "months")}
            <div class="sched-l">几点</div>
            <div class="sched-times">${SCHED.times.map((x, i) => `<span class="sched-time"><input type="time" value="${esc(x)}" data-schedtime="${i}" /><button data-schedrm="${i}" aria-label="删掉这个时刻" ${SCHED.times.length < 2 ? "disabled" : ""}>×</button></span>`).join("")}
              <button class="btn sm" data-schedadd>＋ 加一个时刻</button></div>
          </div>
          <div class="sched-preview">
            ${v ? `<div class="sched-human">${esc(cronHuman(v))}</div>
              <div class="muted small">接下来：${next.length ? next.map((d) => esc(when(d))).join("、") : "一年里都没有：看看选的月份和日子"}</div>
              <div class="sched-yaml mono">schedule: ${esc(JSON.stringify(v))}</div>` : `<div class="alert bad" style="margin:0">${SCHED.freq === "week" && !SCHED.dow.size ? "至少选一个星期几" : SCHED.freq === "month" && !SCHED.dom.size ? "至少选一天" : "至少要有一个时刻"}</div>`}
            ${cu && changed ? `<div class="alert warn" style="margin:8px 0 0">新的时刻 ${esc(when(cu))} 已经过了、之后还没成功跑过：保存后一两分钟内会补跑一次。</div>` : ""}
          </div>
          <div class="sched-foot"><span class="muted small">按 Sparky 本机时间；保存后下一分钟起生效</span>
            <button class="btn" data-schedclose>取消</button><button class="btn primary" data-schedsave ${changed && next.length ? "" : "disabled"}>保存</button></div></div>`;
      }
      function schedOpen(name) {
        const t = taskByName(name);
        if (!t) return;
        const p = schedParse(t.schedule);
        Object.assign(SCHED, { task: name, freq: "day", dow: new Set(), dom: new Set(), mon: new Set(), times: ["08:00"], odd: p ? "" : cronHuman(t.schedule) }, p || {});
        let box = $("#sched");
        if (!box) {
          box = document.createElement("div");
          box.id = "sched";
          box.className = "sched-mask";
          document.body.appendChild(box);
        }
        box.hidden = false;
        box.innerHTML = schedHTML();
      }
      const schedRedraw = () => { const box = $("#sched"); if (box && !box.hidden) box.innerHTML = schedHTML(); };
      const schedClose = () => { const box = $("#sched"); if (box) { box.hidden = true; box.innerHTML = ""; } };
      document.addEventListener("click", async (e) => {
        const g = (k) => e.target.closest(`[data-${k}]`);
        let x;
        if ((x = g("schededit"))) { e.preventDefault(); document.querySelectorAll(".menu").forEach((m) => (m.hidden = true)); return schedOpen(x.dataset.schededit); }
        if (!e.target.closest("#sched")) return;
        if (e.target.id === "sched" || g("schedclose")) return schedClose();
        if ((x = g("schedfreq"))) { SCHED.freq = x.dataset.schedfreq; return schedRedraw(); }
        if ((x = g("schedpick"))) { const [k, n] = x.dataset.schedpick.split(":"); SCHED[k].has(+n) ? SCHED[k].delete(+n) : SCHED[k].add(+n); return schedRedraw(); }
        if ((x = g("schedset"))) { const [k, ns] = x.dataset.schedset.split(":"); SCHED[k] = new Set(ns ? ns.split(",").map(Number) : []); return schedRedraw(); }
        if ((x = g("schedrm"))) { SCHED.times.splice(+x.dataset.schedrm, 1); return schedRedraw(); }
        if (g("schedadd")) { const last = SCHED.times.at(-1) || "08:00"; SCHED.times.push(`${pad((+last.slice(0, 2) + 1) % 24)}:${last.slice(3)}`); return schedRedraw(); }
        if ((x = g("schedsave"))) {
          x.disabled = true;
          try {
            await api(`/api/tasks/${encodeURIComponent(SCHED.task)}/schedule`, { method: "POST", body: JSON.stringify({ schedule: schedBuild() }) });
            toast("已修改定时");
            schedClose();
            D.tasks = await api("/api/tasks");
            render();
          } catch (err) { toast(err.message, true); x.disabled = false; }
        }
      });
      document.addEventListener("input", (e) => {          // 改时刻：只换预览和保存按钮，不重画输入框（免得打断正在选的时间）
        if (!e.target.matches?.("[data-schedtime]")) return;
        SCHED.times[+e.target.dataset.schedtime] = e.target.value;
        const box = document.createElement("div");
        box.innerHTML = schedHTML();
        $("#sched .sched-preview").replaceWith(box.querySelector(".sched-preview"));
        $("#sched [data-schedsave]").outerHTML = box.querySelector("[data-schedsave]").outerHTML;
      });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !($("#sched")?.hidden ?? true)) schedClose(); });

      // ---------- 加 / 撤工具与子代理 ----------
      const ADD = { tool: "", err: "", q: "", kind: "tools" };   // kind：tools 或 subagents（任务 yaml 的哪一段）
      const addWhat = () => (ADD.kind === "subagents" ? "子代理" : "工具");
      function toYaml(v, ind = "") {
        if (Array.isArray(v)) return v.length ? v.map((x) => (x && typeof x === "object" ? `\n${ind}- ${toYaml(x, ind + "  ").trimStart()}` : `\n${ind}- ${JSON.stringify(x)}`)).join("") : " []";
        if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `\n${ind}${k}:${x && typeof x === "object" ? toYaml(x, ind + "  ") : " " + JSON.stringify(x)}`).join("");
        return " " + JSON.stringify(v);
      }
      function addCands(t) {
        if (ADD.kind === "subagents") {
          const have = new Set((t.spec?.subagents || []).map((u) => u.name));
          return D.subagents.filter((x) => x.name && !have.has(x.name));
        }
        const have = new Set((t.tools || []).map((u) => u.name));
        return D.tools.filter((x) => !have.has(x.name) && x.usable_by !== "subagent");   // 只给子代理用的工具任务里不能写
      }
      function pickList(cands) {
        const q = ADD.q.trim().toLowerCase();
        const hit = cands.filter((x) => !q || x.name.toLowerCase().includes(q) || String(x.description || "").toLowerCase().includes(q));
        return hit.length
          ? hit.map((x) => `<button class="${ADD.tool === x.name ? "on" : ""}" data-pick="${esc(x.name)}"><b>${esc(x.name)}</b><span>${esc(String(x.description || "").slice(0, 90))}</span></button>`).join("")
          : `<div class="muted small" style="padding:10px 12px">没有匹配的${addWhat()}</div>`;
      }
      function addPanel(t) {
        const cands = addCands(t);
        const spec = (ADD.kind === "subagents" ? D.subagents : D.tools).find((x) => x.name === ADD.tool);
        return `<div class="addbox">
          <input class="search" data-addsearch type="search" placeholder="搜索${addWhat()}名或说明…" value="${esc(ADD.q)}" autocomplete="off" />
          <div class="picklist" id="picklist" ${ADD.tool && !ADD.q.trim() ? "hidden" : ""}>${pickList(cands)}</div>
          ${spec ? `<div class="picked">${esc(spec.name)}</div><p>${esc(spec.description)}</p>
            ${ADD.err ? `<div class="alert bad" style="margin:12px 0 0">${esc(ADD.err)}</div>` : ""}
            <div class="addacts"><button class="btn primary" data-addgo data-task="${esc(t.name)}">添加</button><button class="btn" data-addclose>取消</button>
            <span class="muted small">${ADD.kind === "subagents" ? "子代理的配置在 subagents/&lt;名&gt;/" : "工具能做什么由它自己定"}；本任务的限制写在系统提示词里</span></div>` : ""}
        </div>`;
      }
      const addTask = () => taskByName(ADD.task || route()[1]);
      const redrawAdd = () => { const box = $("#tool-add"); const t = addTask(); if (box && t) box.innerHTML = addPanel(t); };

      const RUN_LIMIT = {};
      const runSecs = runTotal;
      function runsTable(all, showTask, key, steps) {
        if (!all.length) return `<div class="empty">暂无运行记录</div>`;
        const lim = RUN_LIMIT[key] || 30;
        const rs = all.slice(0, lim);
        const more =
          all.length > lim
            ? `<div style="text-align:center;margin-top:14px"><button class="btn" data-more="${esc(key)}">再显示 30 条<span class="muted">（剩余 ${all.length - lim}）</span></button></div>`
            : "";
        return `<div class="scroll-x"><table><thead><tr>${showTask ? "<th>任务</th>" : ""}<th>开始</th><th>触发</th><th>结果</th><th class="num">耗时</th>${steps ? "<th>步骤</th>" : `<th class="num">调用</th><th class="num">token 入 / 出</th>`}<th>备注</th></tr></thead><tbody>
          ${rs
            .map(
              (
                r,
              ) => `<tr class="link" data-href="#/runs/${esc(r.task)}/${esc(r.run_id)}">${showTask ? `<td class="nowrap"><b>${esc(r.task)}</b></td>` : ""}
            <td class="nowrap">${when(runStart(r))}</td><td class="nowrap">${trigName(r.trigger)}</td><td>${stPill(r.status)}</td>
            <td class="num">${dur(runSecs(r))}</td>${steps ? `<td>${wfDots(r, steps)}</td>` : `<td class="num">${r.calls ?? "—"}${r.rejections ? `<span class="muted">（拒 ${r.rejections}）</span>` : ""}</td>
            <td class="num">${runTok(r) ? `${(runTok(r).input / 1000).toFixed(1)}k / ${(runTok(r).output / 1000).toFixed(1)}k` : "—"}</td>`}
            <td><div class="clip">${esc(runNote(r) || (r.preempt_count ? `被抢占 ${r.preempt_count} 次` : ""))}</div></td></tr>`,
            )
            .join("")}
          </tbody></table></div>${more}`;
      }

      // ---------- 一次运行 ----------
      let RUN_TAB = "calls";
      const RUN_CACHE = {};
      async function viewRun(task, run) {
        let d;
        try {
          d = RUN_CACHE[`${task}/${run}`] = await api(
            `/api/runs/${encodeURIComponent(task)}/${encodeURIComponent(run)}`,
          );
        } catch (e) {
          return `${backLink(`#/tasks/${task}`, task)}<div class="alert bad">无法读取运行记录：${esc(e.message)}</div>`;
        }
        return runHTML(task, d);
      }
      function runHTML(task, d) {
        if (d.workflow) return wfRunHTML(task, d);
        const r = d.run;
        const st = toDate(r.first_started || r.started),
          en = toDate(r.ended);
        const stat = (a, b) =>
          `<div class="stat"><span>${a}</span><b>${b}</b></div>`;
        const calls = d.gateway.filter((e) => e.ev === "call");
        const tabs = [
          ["calls", `工具调用 ${calls.length}`],
          ["talk", `会话 ${d.toollog.length}`],
          ["gateway", `网关事件 ${d.gateway.length}`],
          ["stderr", "stderr"],
          ["yaml", "任务快照"],
        ];
        return `${backLink(`#/tasks/${task}`, task)}
          <div class="title-row"><h1>${esc(task)}</h1>${stPill(r.status)}</div>
          <p class="lead">${trigName(r.trigger)}触发，${when(st)} 开始${en && r.status !== "running" ? `，${when(en)} 结束` : ""}<br><span class="mono">${esc(d.dir)}</span></p>
          ${runNote(r) ? `<div class="alert ${["failed", "timeout"].includes(r.status) ? "bad" : "warn"}">${esc(runNote(r))}</div>` : ""}
          <section class="card"><div class="stats">
            ${stat("总耗时", isCurRun(r) ? `<span data-runtotal="${esc(task)}/${esc(r.run_id)}" data-base="${r.elapsed_seconds || 0}">${dur(runTotal(r))}</span>` : dur(runTotal(r)))}
            ${r.load_seconds ? stat("模型加载", dur(r.load_seconds)) : ""}
            ${r.subagent_seconds ? stat("子代理", dur(r.subagent_seconds)) : ""}
            ${r.paused_seconds ? stat("暂停", dur(r.paused_seconds)) : ""}
            ${stat("工具调用", `${r.calls ?? calls.length} 次`)}
            ${r.rejections ? stat("被拒", `${r.rejections} 次`) : ""}
            ${r.preempt_count ? stat("被抢占", `${r.preempt_count} 次`) : ""}
            ${runTok(r) ? stat("token 入 / 出", `${runTok(r).input.toLocaleString()} / ${runTok(r).output.toLocaleString()}`) : ""}
            ${runTok(r) !== r.tokens && r.tokens ? stat("其中主代理", `${r.tokens.input.toLocaleString()} / ${r.tokens.output.toLocaleString()}`) : ""}
            ${r.memory_peak ? stat("内存峰值", `${r.memory_peak.mem_used_gb} GB`) : ""}
          </div></section>
          ${r.final_text ? `<section class="card"><h3>最终输出</h3><div class="prose" style="margin:0">${esc(r.final_text)}</div></section>` : ""}
          ${d.out_files.length ? `<section class="card"><h3>产出文件（工作目录 out/）</h3><div class="taglist">${d.out_files.map((f) => `<span class="tag mono">${esc(f)}</span>`).join("")}</div></section>` : ""}
          <section class="card"><div class="card-head"><div class="seg">${tabs.map(([k, l]) => `<button class="${RUN_TAB === k ? "on" : ""}" data-runtab="${k}">${l}</button>`).join("")}</div></div>
            <div id="run-body">${runTab(d)}</div></section>`;
      }
      function runTab(d) {
        const t0 = d.gateway[0]?.ts || 0;
        const rel = (ts) =>
          ts
            ? `+${dur(ts - t0)
                .replace(" 秒", "s")
                .replace(/ 分 ?/, "m")
                .replace(" 小时 ", "h")}`
            : "";
        if (RUN_TAB === "calls") {
          const done = new Set(d.gateway.filter((e) => e.ev === "call" && e.call_id).map((e) => e.call_id));
          const calls = d.gateway.filter((e) => e.ev === "call" || (e.ev === "call_start" && !done.has(e.call_id)));   // 平台代调还没结束的（只有开始那条）显示成进行中
          if (!calls.length) return `<div class="empty">无工具调用</div>`;
          // 子代理的调用（网关记 subagent）下面挂它这次调过的工具（subagent_call，各自结束时记，按 call_id 对上；旧记录没有就按这次调用的时间段）
          const inner = (e) =>
            e.subagent
              ? d.gateway.filter((x) => x.ev === "subagent_call" && x.subagent === e.subagent && (x.call_id ? x.call_id === e.call_id : x.ts <= e.ts + 1 && x.ts >= e.ts - (e.seconds || 0) - 1))
              : [];
          const innerHTML = (xs) =>
            xs.length
              ? `<div class="subtr">${xs.map((x) => `<div class="st-row ${x.ok || x.guide ? "" : "bad"}"><i class="st-dot"></i><span class="st-act">${esc(x.tool)}</span><span class="st-res">${x.guide ? "先返回用法规范" : x.ok ? "成功" : esc(errName(x.code))}${x.seconds != null ? `，${dur(x.seconds)}` : ""}${x.result_chars != null ? `，返回 ${x.result_chars.toLocaleString()} 字` : ""}${x.cards?.length ? `，等卡 ${x.cards.length} 张` : ""}</span></div>${x.message && !x.ok ? `<div class="st-think">${esc(x.message)}</div>` : ""}`).join("")}</div>`
              : "";
          return calls
            .map(
              (
                e,
              ) => `<div class="ev"><div class="at">${rel(e.ts)}</div><div class="what">
            <div class="tool-head"><b>${esc(e.tool)}</b>${e.subagent ? `<span class="tag acc">子代理</span>` : ""}${e.ev === "call_start" ? stPill("running") : e.guide ? `<span class="st waiting">先返回用法规范</span>` : e.timeout ? `<span class="st timeout">超时</span>` : e.ok ? `<span class="st complete" data-ico="✓">成功</span>` : `<span class="st failed" data-ico="✕">${esc(errName(e.code))}</span>`}
            <span class="muted small">${e.seconds != null ? dur(e.seconds) : ""}${e.origin && e.origin !== "agent" ? `，来源 ${esc(e.origin)}` : ""}${e.model_calls ? `，子代理的模型调用 ${e.model_calls} 次` : ""}${e.result_chars != null ? `，返回 ${e.result_chars.toLocaleString()} 字` : ""}</span></div>
            ${e.message ? `<div class="muted small">${esc(e.message)}</div>` : ""}${innerHTML(inner(e))}</div></div>`,
            )
            .join("");
        }
        if (RUN_TAB === "talk") {
          if (!d.toollog.length)
            return `<div class="empty">无会话记录（workflow 任务不产生）</div>`;
          return d.toollog
            .map((e) => {
              const at = `<div class="at">${e.t != null ? `${Math.round(e.t)}s` : ""}</div>`;
              if (e.ev === "assistant")
                return `<div class="ev">${at}<div class="what"><b>模型</b> <span class="muted small">入 ${e.input?.toLocaleString?.() ?? e.input} / 出 ${e.output} token${e.calls?.length ? `，调用 ${esc(e.calls.join("、"))}` : ""}</span>${e.text ? `<div class="prose">${esc(e.text)}</div>` : ""}</div></div>`;
              if (e.ev === "tool_start")
                return `<div class="ev">${at}<div class="what"><b>调用 ${isShell(e) ? `Shell${e.args.description ? `：${esc(e.args.description)}` : ""}` : esc(e.tool)}</b><pre class="code">${isShell(e) ? esc(e.args.command).slice(0, 4000) : esc(JSON.stringify(e.args, null, 2)).slice(0, 4000)}</pre></div></div>`;
              if (e.ev === "tool_end")
                return `<div class="ev">${at}<div class="what"><b>${esc(e.tool)} 返回</b> <span class="muted small">${e.ms != null ? dur(e.ms / 1000) : ""}</span>${e.error ? ` <span class="st failed" data-ico="✕">出错</span>` : ""}<div class="prose">${esc(String(e.result ?? "").slice(0, 4000))}</div></div></div>`;
              return `<div class="ev">${at}<div class="what muted">${esc(e.ev)}</div></div>`;
            })
            .join("");
        }
        if (RUN_TAB === "gateway") {
          return d.gateway.length
            ? `<div class="scroll-x"><table><thead><tr><th>时间</th><th>事件</th><th>工具</th><th>结果</th><th>备注</th></tr></thead><tbody>${d.gateway.map((e) => `<tr><td class="nowrap">${rel(e.ts)}</td><td>${esc(e.ev)}</td><td>${esc(e.ev === "subagent_call" ? `${e.subagent} → ${e.tool || ""}` : e.tool || "")}</td><td>${e.ev === "call" || e.ev === "subagent_call" ? (e.ok ? "成功" : esc(errName(e.code))) : ""}</td><td class="muted">${esc([e.origin, e.message].filter(Boolean).join("，"))}</td></tr>`).join("")}</tbody></table></div>`
            : `<div class="empty">无网关记录</div>`;
        }
        if (RUN_TAB === "stderr")
          return `<h3>agent</h3><pre class="code">${esc(d.agent_stderr || "（空）")}</pre><h3>网关</h3><pre class="code">${esc(d.gateway_stderr || "（空）")}</pre>`;
        if (RUN_TAB === "yaml")
          return `<pre class="code">${esc(d.task_yaml || "（未保存）")}</pre>`;
        return "";
      }

      const isSubTool = (x) => x?.usable_by === "subagent";       // 工具页：只给子代理用的工具和普通工具分开列

      // ---------- 工具页 ----------
      // 平台函数：control_center/tools/ 下的平台工具（/api/tools 的 platform），没有 MCP 服务器、不起进程
      const platTag = (t) => (t?.platform ? `<span class="tag plat" title="平台工具（control_center/tools/）：函数由沙箱或网关自己处理，没有 MCP 服务器、不起进程">平台函数</span>` : "");
      const usedBy = (tool) =>
        D.tasks
          .filter((t) => (t.tools || []).some((u) => u.name === tool))
          .map((t) => t.name);
      // 列了加载工具（/api/tools 的 loader）的任务：能加载的工具和子代理（loadable）平台都算它们用到的
      const loaders = () => {
        const ls = D.tools.filter((t) => t.loader).map((t) => t.name);
        return D.tasks.filter((t) => (t.tools || []).some((u) => ls.includes(u.name))).map((t) => t.name);
      };
      // 使用的任务：直接列了的 + （能加载时）列了加载工具、还没直接列的
      const taskUsers = (direct, loadable) => {
        const via = loadable ? loaders().filter((n) => !direct.includes(n)) : [];
        const ln = D.tools.find((t) => t.loader)?.name;
        const parts = [direct.length ? taskLinks(direct) : "", via.length ? `${taskLinks(via)}<span class="muted">（经 <a href="#/tools/${esc(ln)}">${esc(ln)}</a> 加载）</span>` : ""];
        return parts.filter(Boolean).join("；") || "无";
      };
      const taskLinks = (ns) =>
        ns.length
          ? ns
              .map((n) => `<a href="#/tasks/${esc(n)}"><b>${esc(n)}</b></a>`)
              .join("、")
          : "无";
      function toolCard(t) {
        return `<div class="tool" id="tool-${esc(t.name)}"><div class="tool-head"><a href="#/tools/${esc(t.name)}"><b>${esc(t.name)}</b></a>
            ${platTag(t)}${t.usable_by === "subagent" ? `<span class="tag">只给子代理用</span>` : ""}</div>
          <p>${esc(t.description)}</p>
          <div class="small muted" style="margin-top:8px">使用方：${isSubTool(t) ? "" : taskUsers(usedBy(t.name), t.loadable)}${(t.subagents || []).length ? `${isSubTool(t) ? "" : "；"}子代理 ${t.subagents.map((n) => `<a href="#/subagents/${esc(n)}"><b>${esc(n)}</b></a>`).join("、")}` : isSubTool(t) ? "无" : ""}</div></div>`;
      }
      // load-tool 的目录：按列了它的任务分开——每个任务能加载的 = 全部可加载的减去它已直接列出的（和平台写进它系统提示的一样）
      const CAT = { x: null, task: "" };
      function catHTML(x) {
        const users = usedBy(x.name);
        const task = users.includes(CAT.task) ? CAT.task : users[0];
        const t = taskByName(task);
        const have = new Set([...(t?.tools || []), ...(t?.spec?.subagents || [])].map((u) => u.name));
        const can = x.catalog.filter((c) => !have.has(c.name)), direct = x.catalog.filter((c) => have.has(c.name));
        const link = (c) => `<a href="#/${c.kind === "subagent" ? "subagents" : "tools"}/${esc(c.name)}"><b>${esc(c.name)}</b></a>`;
        return `<h3 style="margin:20px 0 4px">能加载的工具和子代理${task ? `<span class="hint" style="margin-left:8px">${can.length} 个</span>` : ""}</h3>
          ${!task ? `<p class="small muted" style="margin:0 0 8px">还没有任务列 ${esc(x.name)}；列了的任务，系统提示里写着它没直接列出的那些，用到时按名字加载。全部可加载的 ${x.catalog.length} 个：</p>`
            : `<p class="small muted" style="margin:0 0 8px">${users.length > 1 ? `<span class="seg" style="margin-right:8px">${users.map((n) => `<button class="${n === task ? "on" : ""}" data-cattask="${esc(n)}">${esc(n)}</button>`).join("")}</span>` : `<a href="#/tasks/${esc(task)}"><b>${esc(task)}</b></a> `}系统提示里写着这份目录，用到时按名字加载${direct.length ? `；它已经直接列出、不用加载的：${direct.map(link).join("、")}` : ""}</p>`}
          ${(task ? can : x.catalog).map((c) => `<div class="tool"><div class="tool-head">${link(c)}${c.kind === "subagent" ? `<span class="tag acc">子代理</span>` : ""}</div><p>${esc(c.description)}</p></div>`).join("")}`;
      }
      // 一个工具的详情页（#/tools/<名>）：按 /api/tools/<名> 给的字段统一渲染（函数、联网、容器、依赖、凭证键名、hooks、guide.md、使用方）
      async function viewTool(name) {
        let x;
        try {
          x = await api(`/api/tools/${encodeURIComponent(name)}`);
        } catch (e) {
          return `${backLink("#/tools", "全部工具")}<h1>${esc(name)}</h1><div class="alert bad">${esc(e.message)}</div>`;
        }
        const mono = (xs) => xs.map((n) => `<span class="mono">${esc(n)}</span>`).join("、");
        const fns = x.functions || [], dk = x.docker;
        const rows = [
          ["使用范围", isSubTool(x) ? `只给子代理用<span class="sub">列在子代理的 tools 里，任务 yaml 不能直接列</span>` : "任务和子代理都能列"],
          ["函数", fns.length ? `<div class="small">${fns.map((f) => `<div><b class="mono">${esc(f.name)}</b>${f.description ? `<span class="muted">：${esc(f.description)}</span>` : ""}</div>`).join("")}</div>` : "无"],
          ["联网", x.network === true ? "可以" : x.network === false ? "不可以" : `只放行本机端口 ${mono(x.network.map(String))}`],
          ["容器", x.builtin ? `在沙箱容器里<span class="sub">函数是沙箱里 pi 自带的，没有 MCP 服务器</span>` : x.gateway ? `不用<span class="sub">函数由网关自己处理，没有 MCP 服务器</span>` : dk ? `${mono(dk.images || [])}<span class="sub">内存 ${esc(dk.memory)}，CPU ${esc(dk.cpus)}${dk.gpu ? "，GPU" : ""}${dk.timeout ? `，预计 ${esc(dk.timeout)}` : ""}</span>` : "不用（在宿主上跑）"],
          ["依赖", x.deps.length ? mono(x.deps) : x.builtin ? "沙箱的环境" : x.gateway ? "无" : "平台的 venv"],
          ["凭证", x.credentials.length ? `${mono(x.credentials)}<span class="sub">.env 的键名，用到时工具自己读</span>` : "无"],
          x.hooks.length ? ["hooks", mono(x.hooks)] : null,
          ["修改", x.is_editable ? "builder 计划可改" : "只由 Joy 手动改（is_editable: false）"],
          ["文件夹", `<span class="mono">${esc(x.folder)}</span>${x.platform ? `<span class="sub">平台自带的工具</span>` : ""}`],
          ["使用的任务", isSubTool(x) ? "—" : taskUsers(usedBy(x.name), D.tools.find((t) => t.name === x.name)?.loadable)],
          ["使用的子代理", x.subagents.length ? x.subagents.map((n) => `<a href="#/subagents/${esc(n)}"><b>${esc(n)}</b></a>`).join("、") : "无"],
        ].filter(Boolean);
        return `${backLink("#/tools", "全部工具")}<section class="card"><div class="card-head"><h2>${esc(x.name)}</h2>${platTag(x)}${isSubTool(x) ? `<span class="tag">只给子代理用</span>` : ""}<span class="hint spacer">${fns.length} 个函数</span></div>
          <p style="margin:-8px 0 16px;color:var(--ink-2);max-width:80ch">${esc(x.description)}</p>
          <dl>${rows.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>
          ${x.catalog ? `<div id="cat">${catHTML((CAT.x = x))}</div>` : ""}
          ${x.guide ? `<details style="margin-top:12px"><summary>${x.platform ? "用法规范（guide.md：写进列了它的任务的系统提示）" : "用法规范（guide.md：会话里第一次调用时网关先返回它）"}</summary><div class="prose">${esc(x.guide)}${x.guide_len > x.guide.length ? `\n…（共 ${x.guide_len} 字，只显示前 ${x.guide.length} 字）` : ""}</div></details>` : ""}</section>`;
      }
      function viewTools(focus) {
        if (focus) return viewTool(focus);
        const sub = D.tools.filter(isSubTool),
          plat = D.tools.filter((t) => t.platform && !isSubTool(t)),
          plain = D.tools.filter((t) => !t.platform && !isSubTool(t));
        return `<h1>工具</h1><p class="lead">工具运行在宿主上（平台函数由沙箱或网关自己处理），用到凭证时自己从 .env 读。Agent 只能经网关调用任务授权的工具；工具能做什么由它自己定，任务的限制写在系统提示词里。</p>
          ${plat.length ? `<section class="card"><div class="card-head"><h2>平台函数</h2><span class="hint">${plat.length} 个，在 control_center/tools/，和别的工具一样任务列了才有</span></div>${plat.map(toolCard).join("")}</section>` : ""}
          <section class="card"><div class="card-head"><h2>普通工具</h2><span class="hint">${plain.length} 个</span></div>${plain.map(toolCard).join("")}</section>
          <section class="card"><div class="card-head"><h2>子代理工具</h2><span class="hint">${sub.length} 个，只给子代理用（列在子代理的 tools 里），配置见「子代理」</span></div>${sub.map(toolCard).join("")}</section>`;
      }

      // ---------- Skills 页 ----------
      // skills/ 下装好的 skill：总览取 /api/skills，详情（#/skills/<名>）取 /api/skills/<名>；经 skills 工具搜索、加载、导入
      async function viewSkills(name) {
        let x;
        try {
          x = await api(name ? `/api/skills/${encodeURIComponent(name)}` : "/api/skills");
        } catch (e) {
          return `${name ? backLink("#/skills", "全部 skill") : ""}<h1>${esc(name || "Skills")}</h1><div class="alert bad">${esc(e.message)}</div>`;
        }
        const mono = (xs) => xs.map((n) => `<span class="mono">${esc(n)}</span>`).join("、");
        if (!name)
          return `<h1>Skills</h1><p class="lead">别人写好的做法（SKILL.md，可带脚本和参考文档），放在仓库 skills/。任务列了 skills 工具才能用：先搜索、再按名字加载；导入新的要 Joy 批准。</p>
            <section class="card"><div class="card-head"><h2>已装的 skill</h2><span class="hint">${x.length} 个</span></div>${
              x.length ? x.map((k) => `<div class="tool"><div class="tool-head"><a href="#/skills/${esc(k.name)}"><b>${esc(k.name)}</b></a></div><p>${esc(k.description)}</p></div>`).join("") : `<div class="empty">还没有装 skill。</div>`
            }<div class="small muted" style="margin-top:8px">能用的任务：${taskUsers(usedBy("skills"), D.tools.find((t) => t.name === "skills")?.loadable)}</div></section>
            <section class="card"><div class="card-head"><h2>导入 skill</h2></div>
              <p class="small muted" style="margin:0 0 12px">上传一个 .zip、一个 SKILL.md 或整个 skill 文件夹（不超过 20 MB）：直接装进 skills/ 并提交 git，不发审批卡；要装的 Python 包接着在后台装进沙箱的环境。</p>
              <div style="display:flex;gap:8px"><label class="btn sm">选文件<input type="file" accept=".zip,.skill,.md" data-skillup hidden /></label><label class="btn sm">选文件夹<input type="file" webkitdirectory data-skillup hidden /></label></div></section>`;
        const rows = [
          ["文件", `<div class="taglist">${x.files.map((f) => `<span class="tag mono">${esc(f)}</span>`).join("")}</div>`],
          ["Python 包", x.requirements.length ? `${mono(x.requirements)}<span class="sub">${
            !x.deps ? "导入时装进沙箱的环境" : x.deps.state === "installing" ? "正在后台装进沙箱的环境…（刷新看结果）" : x.deps.state === "failed" ? `没装成：${esc(x.deps.error)}`
              : x.deps.failed.length ? `这些没装上：${esc(x.deps.failed.join("、"))}` : "已装进沙箱的环境"}</span>` : "无"],
          ["文件夹", `<span class="mono">${esc(x.folder)}</span>`],
        ];
        return `${backLink("#/skills", "全部 skill")}<section class="card"><div class="card-head"><h2>${esc(x.name)}</h2><span class="hint spacer">${x.files.length} 个文件</span></div>
          <p style="margin:-8px 0 16px;color:var(--ink-2);max-width:80ch">${esc(x.description)}</p>
          <dl>${rows.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>
          <details style="margin-top:12px"><summary>做法（SKILL.md：load_skill 交给模型的内容）</summary><div class="prose">${esc(x.body)}${x.body_len > x.body.length ? `\n…（共 ${x.body_len} 字，只显示前 ${x.body.length} 字）` : ""}</div></details></section>`;
      }
// ---------- API 页 ----------
// 算力 API（control_center/api_door.py）：每个 API 是一个任务 api-<名>，绑本机一个模型档位、一个端口、一把密钥；数据取 /api/apis。
// 新建：名称、模型（models.yaml 里有服务的档位，接口给的菜单）、优先级、本地 / 公网 → 生成端口和密钥（密钥原文只显示这一次）
const API_ST = {
  running: ["会话在跑", "running"],
  loading: ["正在加载模型", "stepping"],
  queued: ["排队中", "queued"],
  idle: ["没有会话", "complete"],
};
const API_FORM = { open: false, name: "", tier: "", priority: "", public: false, err: "" };
let API_D = null;
const wan = (n) =>
  n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n || 0);
function apiRow(a) {
  const [label, cls] = API_ST[a.state] || [a.state, ""];
  const where = [
    `<span class="mono">${esc(a.urls.local)}</span>`,
    a.urls.public ? `<span class="mono">${esc(a.urls.public)}</span>` : "",
  ]
    .filter(Boolean)
    .join("<br>");
  const s = a.session;
  const now = s
    ? `${when(toDate(s.started))} 起，在答 ${s.inflight || 0} 路`
    : a.waiting ||
      (a.last_request ? `最后请求 ${ago(toDate(a.last_request))}` : "还没用过");
  const fail =
    a.last_run && a.last_run.status === "failed"
      ? `<div class="alert bad" style="margin:8px 0 0">上次会话失败：${esc(a.last_run.error || "")}</div>`
      : "";
  return `<div class="tool"><div class="tool-head"><a href="#/tasks/${esc(a.name)}"><b>${esc(a.name)}</b></a>
            <span class="st ${cls}" data-ico="●">${esc(label)}</span><span class="spacer"></span>
            ${["running", "loading"].includes(a.state) ? `<button class="btn sm" data-apiact="stop" data-api="${esc(a.name)}">结束会话</button>` : ""}
            <button class="btn sm" data-apiact="rekey" data-api="${esc(a.name)}">换密钥</button>
            <button class="btn sm" data-apiact="delete" data-api="${esc(a.name)}">删除</button></div>
          ${
            a.error
              ? `<div class="alert bad">任务读不了：${esc(a.error)}</div>`
              : `<dl>
            <dt>模型</dt><dd><span class="mono">${esc(a.model)}</span><span class="sub">档位 ${esc(a.tier)}</span></dd>
            <dt>优先级</dt><dd>${esc(a.priority)}${a.enabled ? "" : `<span class="sub">任务已停用</span>`}</dd>
            <dt>地址</dt><dd>${where}<span class="sub">${a.public ? "公网（Funnel）+ tailnet" : "本地（tailnet）"} · 端口 ${esc(a.port)}</span></dd>
            <dt>现在</dt><dd>${esc(now)}</dd>
            <dt>今天</dt><dd>${a.today.requests} 次请求 · 输入 ${wan(a.today.input)} / 输出 ${wan(a.today.output)} token</dd></dl>`
          }${fail}</div>`;
}
function apiUsage(x) {
  const max = Math.max(1, ...x.daily.map((d) => d.tokens));
  const daily = x.daily.length
    ? `<div class="scroll-x"><table><thead><tr><th>日期</th><th>API</th><th class="num">请求</th><th class="num">token</th><th></th></tr></thead><tbody>${x.daily
        .slice()
        .reverse()
        .map(
          (d) =>
            `<tr><td class="nowrap">${esc(d.day)}</td><td>${esc(d.api)}</td><td class="num">${d.requests}</td><td class="num">${wan(d.tokens)}</td>
           <td style="width:40%"><div style="height:8px;border-radius:4px;background:var(--accent);width:${(d.tokens / max) * 100}%"></div></td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : `<div class="empty">近 30 天没有请求</div>`;
  const recent = x.recent.length
    ? `<div class="scroll-x"><table><thead><tr><th>时刻</th><th>API</th><th>请求</th><th>状态</th><th class="num">token 入 / 出</th><th class="num">用时</th></tr></thead><tbody>${x.recent
        .map(
          (r) =>
            `<tr><td class="nowrap">${when(toDate(r.ts))}</td><td>${esc(r.api)}</td><td class="mono">${esc(r.method)} ${esc(r.path)}</td><td>${esc(r.status)}</td>
           <td class="num">${r.input || r.output ? `${wan(r.input)} / ${wan(r.output)}` : "—"}</td><td class="num">${dur((r.ms || 0) / 1000)}</td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : `<div class="empty">还没有请求</div>`;
  return `<section class="card"><div class="card-head"><h2>每天的用量</h2><span class="hint">近 30 天，token = 输入 + 输出</span></div>${daily}</section>
          <section class="card"><div class="card-head"><h2>最近的请求</h2><span class="hint">最近 50 次；不记内容</span></div>${recent}</section>`;
}
function apiForm(x) {
  if (!API_FORM.tier && x.models.length) API_FORM.tier = x.models[0].tier;
  return `<div class="sched-l">名称</div><div style="display:flex;align-items:center;gap:6px"><span class="mono">${esc(x.prefix)}</span>
            <input class="search" style="flex:1" data-apif="name" value="${esc(API_FORM.name)}" autocomplete="off" /></div>
          <div class="sched-l">模型</div><select data-apif="tier" style="width:100%">${x.models
            .map(
              (m) =>
                `<option value="${esc(m.tier)}" ${API_FORM.tier === m.tier ? "selected" : ""}>${esc(m.tier)} · ${esc(m.model)}${m.vision ? "（能看图）" : ""}</option>`,
            )
            .join("")}</select>
          <div class="sched-l">优先级</div><input type="number" min="1" max="9" step="1" data-apif="priority" value="${esc(API_FORM.priority)}" placeholder="1–9" class="search" style="width:110px" />
          <div class="small muted" style="margin-top:6px">大的先跑、能抢占比它小的。</div>
          <div class="sched-l">对外地址</div><div class="seg"><button class="${API_FORM.public ? "" : "on"}" data-apipub="0">本地（tailnet）</button><button class="${API_FORM.public ? "on" : ""}" data-apipub="1">公网（Funnel）</button></div>
          ${API_FORM.err ? `<div class="alert bad" style="margin:12px 0 0">${esc(API_FORM.err)}</div>` : ""}
          <div style="margin-top:16px"><button class="btn primary" data-apicreate>生成端口和密钥</button></div>`;
}
async function viewApis() {
  try {
    API_D = await api("/api/apis");
  } catch (e) {
    return `<h1>API</h1><div class="alert bad">${esc(e.message)}</div>`;
  }
  const x = API_D;
  return `<h1>API</h1><p class="lead">用 Sparky 本机的一个模型，像 OpenAI 的 API key 一样从别的设备调用。每个 API 是一个任务，请求来了它的会话照常排队、被打断；${x.idle_minutes} 分钟没有请求会话就结束，机器让给别的任务。</p>
          ${
            API_FORM.open
              ? `<section class="card"><div class="card-head"><h2>新建 API</h2><span class="spacer"></span><button class="btn sm" data-apinew>取消</button></div><div id="api-form">${apiForm(x)}</div></section>`
              : `<div style="margin-bottom:16px"><button class="btn primary" data-apinew>新建 API</button></div>`
          }
          <section class="card"><div class="card-head"><h2>全部 API</h2><span class="hint">${x.apis.length} 个</span></div><div id="api-list">${x.apis.length ? x.apis.map(apiRow).join("") : `<div class="empty">还没有 API</div>`}</div></section>
          <div id="api-usage">${apiUsage(x)}</div>`;
}
async function refreshApis() {
  if (!$("#api-list") || document.querySelector(".sched-mask")) return;
  try {
    API_D = await api("/api/apis");
  } catch {
    return;
  }
  if (!$("#api-list")) return;
  $("#api-list").innerHTML = API_D.apis.length
    ? API_D.apis.map(apiRow).join("")
    : `<div class="empty">还没有 API</div>`;
  $("#api-usage").innerHTML = apiUsage(API_D);
}
function copyText(t) {
  const ta = document.createElement("textarea"); // 网页走 http（tailnet 地址），clipboard 接口用不了：选中再复制
  ta.value = t;
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    toast("已复制");
  } catch {
    toast("复制不了，手动选中复制", true);
  }
  ta.remove();
}
function apiKeyBox(r) {
  const bases = [r.urls.local, r.urls.public].filter(Boolean);
  const where = (b) => (b === r.urls.public ? " · 公网" : " · 本地");
  const line = (label, v) =>
    `<div class="sched-l">${label}</div><div style="display:flex;gap:6px;align-items:center"><code class="mono" style="flex:1;word-break:break-all">${esc(v)}</code><button class="btn sm" data-copy="${esc(v)}">复制</button></div>`;
  const box = document.createElement("div");
  box.className = "sched-mask";
  box.innerHTML = `<div class="sched-card" role="dialog" aria-label="密钥"><div class="sched-head"><h2>${esc(r.name)}</h2><span class="muted">密钥只显示这一次</span><button class="iconx" data-keyclose aria-label="关闭">×</button></div>
          <div class="sched-body">${
            r.runtime === "comfyui"
              ? bases.map((b) => line(`ComfyUI 地址（密钥已含在地址里）${where(b)}`, `${b}/k/${r.key}/`)).join("")
              : line("API Key", r.key) + bases.map((b) => line(`Base URL（OpenAI 兼容：酒馆、AIRI 选 OpenAI Compatible）${where(b)}`, `${b}/v1`)).join("")
          }
          </div><div class="sched-foot" style="padding:16px 20px"><button class="btn primary" data-keyclose>记下了</button></div></div>`;
  document.body.appendChild(box);
}
document.addEventListener("input", (e) => {
  const f = e.target.dataset?.apif;
  if (f) API_FORM[f] = e.target.value;
});
document.addEventListener("change", (e) => {
  const f = e.target.dataset?.apif;
  if (f) API_FORM[f] = e.target.value;
});
document.addEventListener("click", async (e) => {
  const cp = e.target.closest("[data-copy]");
  if (cp) return copyText(cp.dataset.copy);
  if (e.target.closest("[data-keyclose]")) {
    document.querySelector(".sched-mask")?.remove();
    refreshApis();
    return;
  }
  const pub = e.target.closest("[data-apipub]");
  if (pub) {
    API_FORM.public = pub.dataset.apipub === "1";
    $("#api-form").innerHTML = apiForm(API_D);
    return;
  }
  if (e.target.closest("[data-apinew]")) {
    Object.assign(API_FORM, { open: !API_FORM.open, err: "" });
    $("#view").innerHTML = await viewApis();
    return;
  }
  const cr = e.target.closest("[data-apicreate]");
  if (cr) {
    cr.disabled = true;
    try {
      const r = await api("/api/apis/create", {
        method: "POST",
        body: JSON.stringify({
          name: API_FORM.name,
          tier: API_FORM.tier,
          priority: API_FORM.priority,
          public: API_FORM.public,
        }),
      });
      Object.assign(API_FORM, { open: false, name: "", priority: "", err: "" });
      $("#view").innerHTML = await viewApis();
      apiKeyBox(r);
    } catch (err) {
      API_FORM.err = err.message;
      $("#api-form").innerHTML = apiForm(API_D);
    }
    return;
  }
  const ac = e.target.closest("[data-apiact]");
  if (!ac) return;
  const act = ac.dataset.apiact,
    name = ac.dataset.api;
  if (act !== "stop" && !ac.dataset.armed) {
    // 换密钥、删除点两下：第一下变成「确认…」
    const old = ac.textContent;
    ac.dataset.armed = 1;
    ac.textContent = act === "delete" ? "确认删除" : "确认换密钥";
    ac.classList.add("danger");
    setTimeout(() => {
      delete ac.dataset.armed;
      ac.textContent = old;
      ac.classList.remove("danger");
    }, 3000);
    return;
  }
  ac.disabled = true;
  try {
    const r = await api(`/api/apis/${encodeURIComponent(name)}/${act}`, {
      method: "POST",
      body: "{}",
    });
    if (act === "rekey") apiKeyBox(r);
    else toast(act === "delete" ? `已删除 ${name}` : `已请 ${name} 的会话结束`);
    if (act === "delete") {
      D.tasks = await api("/api/tasks");
      $("#view").innerHTML = await viewApis();
    } else refreshApis();
  } catch (err) {
    toast(err.message, true);
    ac.disabled = false;
  }
});

      // ---------- 子代理页 ----------
      // 每个子代理按 /api/subagents 给的字段统一渲染（种类、模型、单次上限、隔离、工具、参数、程序、guide.md…），新加的子代理不用改这里
      const KIND = { agent: "agent（语言模型带工具）", generator: "generator（生成程序）", engine: "engine（自带循环的程序）" };
      // 一个子代理工具（/api/subagents 的 tools 项）：说明 + 函数清单（网关缓存有说明就带上，否则只有函数名）
      const subToolBlock = (x) => `<div class="tool"><div class="tool-head"><a href="#/tools/${esc(x.name)}"><b>${esc(x.name)}</b></a>
          ${x.missing ? `<span class="tag warn">没有这个工具</span>` : x.usable_by === "subagent" ? `<span class="tag">只给子代理用</span>` : ""}<span class="hint spacer">${(x.functions || []).length} 个函数</span></div>
          ${x.description ? `<p>${esc(x.description)}</p>` : ""}
          ${(x.functions || []).length ? `<details style="margin-top:6px"><summary class="small">函数</summary><div class="small">${x.functions
            .map((fn) => `<div><b class="mono">${esc(fn.name)}</b>${fn.description ? `<span class="muted">：${esc(fn.description)}</span>` : ""}</div>`).join("")}</div></details>` : ""}</div>`;
      // 一个参数（inputs 项，原样的 JSON Schema）：类型、默认值、上下限、可选值
      const inputMeta = (x) => {
        const ty = x.type === "array" && x.items?.type ? `array<${x.items.type}>` : x.type || "";
        const b = [];
        if ("minimum" in x || "maximum" in x) b.push(`${"minimum" in x ? x.minimum : ""}–${"maximum" in x ? x.maximum : ""}`);
        if ("maxItems" in x) b.push(`最多 ${x.maxItems} 项`);
        if (x.enum) b.push(`可选 ${x.enum.map((v) => JSON.stringify(v)).join(" / ")}`);
        return [ty, "default" in x ? `默认 ${JSON.stringify(x.default)}` : "必填", ...b].filter(Boolean).join("，");
      };
      // 一个子代理的卡片：总览页逐个列，详情页（#/subagents/<名>）只列这一张
      function subCard(a, link) {
        const tags = (xs) => `<div class="taglist">${xs.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>`;
        const users = D.tasks.filter((t) => (t.spec?.subagents || []).some((u) => u.name === a.name)).map((t) => t.name);
        const inputs = a.inputs || [];
        const rows = [
          ["种类", `${esc(KIND[a.kind] || a.kind)}${a.server ? `<span class="sub">程序 ${esc(a.server)}</span>` : ""}`],
          [
            "模型",
            a.runtime === "inherit"
              ? "继承调用方"
              : `${esc(shortModel(a.model))}<span class="sub">${esc(RUNTIME[a.runtime] || a.runtime)}，调用时切换模型（往返约 4–5 分钟）</span>`,
          ],
          ["单次上限", a.timeout == null ? "不限" : `${dur(a.timeout)}<span class="sub">不含审批等待</span>`],
          ["隔离", a.isolated && a.prevalidated
            ? `是<span class="sub">读外部内容：程序本身是隔离边界（沙箱里按 schema 校验过的结果），结果原样交给主模型，不过问答引文核对</span>`
            : a.isolated
            ? `是<span class="sub">读外部内容：结果只有问题照抄主模型、引文逐字核对过的答案</span>`
            : "否"],
          ["修改", a.is_editable ? "builder 计划可改" : "只由 Joy 手动改（is_editable: false）"],
          ["工具", a.kind !== "agent" ? "—（程序自己干活，参数见下）" : a.tools.length ? a.tools.map(subToolBlock).join("") : "无"],
          ["参数", `<div class="small"><div><b class="mono">prompt</b>：交给子代理的任务、问题、侧重点</div>${a.kind === "agent" ? `<div><b class="mono">files</b>：可附工作目录下的文件</div>` : ""}${inputs
            .map((x) => `<div><b class="mono">${esc(x.name)}</b> <span class="muted">${esc(inputMeta(x))}</span>：${esc(x.description || "")}</div>`)
            .join("")}</div>`],
          [a.output || a.hooks?.length ? "约定文件" : null, [a.output && "output.py（检查 / 整理结果）", ...(a.hooks || []).map((h) => `hook ${h}`)].filter(Boolean).length
            ? tags([a.output && "output.py（检查 / 整理结果）", ...(a.hooks || []).map((h) => `hook ${h}`)].filter(Boolean)) : ""],
          ["使用方", taskUsers(users, a.loadable)],
        ].filter(([x]) => x);
        return `<section class="card"><div class="card-head"><h2>${link ? `<a href="#/subagents/${esc(a.name)}">${esc(a.name)}</a>` : esc(a.name)}</h2>${a.isolated ? `<span class="tag acc">隔离</span>` : ""}<span class="tag">${esc(a.kind)}</span></div>
        <p style="margin:-8px 0 16px;color:var(--ink-2);max-width:80ch">${esc(a.description)}</p>
        <dl>${rows.map(([x, y]) => `<dt>${x}</dt><dd>${y}</dd>`).join("")}</dl>
        ${a.prompt ? `<details style="margin-top:12px"><summary>系统提示词</summary><div class="prose">${esc(a.prompt)}</div></details>` : ""}
        ${a.guide ? `<details style="margin-top:12px"><summary>用法规范（guide.md：主模型第一次调用时先拿到）</summary><div class="prose">${esc(a.guide)}</div></details>` : ""}</section>`;
      }
      function viewSubagents(name) {
        if (D.subagents[0]?.error)
          return `<h1>子代理</h1><div class="alert bad">配置有误：${esc(D.subagents[0].error)}</div>`;
        if (name) {
          const a = D.subagents.find((x) => x.name === name);
          return `${backLink("#/subagents", "全部子代理")}${a ? subCard(a, false) : `<h1>${esc(name)}</h1><div class="alert bad">没有这个子代理。</div>`}`;
        }
        return `<h1>子代理</h1><p class="lead">主模型可调用的独立会话：用自己的模型、系统提示、工具或程序做完再交回。配置在 subagents/&lt;名&gt;/；
          没写 is_editable: false 的可由 builder 的计划修改（Joy 批审批卡），隔离子代理只由 Joy 手动改。</p>
          ${D.subagents.map((a) => subCard(a, true)).join("")}`;
      }

      // ---------- 日志页 ----------
      const LOG = { tab: "runs", task: "", status: "", unit: "stbcc-runner" };
      const logRuns = () =>
        D.runs.filter(
          (r) =>
            (!LOG.task || r.task === LOG.task) &&
            (!LOG.status || r.status === LOG.status),
        );
      function viewLogs() {
        const names = [...new Set(D.runs.map((r) => r.task))].sort();
        const head = `<h1>日志</h1><p class="lead">运行记录保留 90 天。</p>
          <div class="filters"><div class="seg">${[
            ["runs", "运行记录"],
            ["journal", "服务日志"],
          ]
            .map(
              ([k, l]) =>
                `<button class="${LOG.tab === k ? "on" : ""}" data-logtab="${k}">${l}</button>`,
            )
            .join("")}</div>`;
        if (LOG.tab === "runs") {
          const rs = logRuns();
          return (
            head +
            `<select data-logf="task"><option value="">全部任务</option>${names.map((n) => `<option ${LOG.task === n ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
            <select data-logf="status"><option value="">全部结果</option>${Object.entries(
              STATUS,
            )
              .filter(([k]) => k !== "queued")
              .map(
                ([k, [l]]) =>
                  `<option value="${k}" ${LOG.status === k ? "selected" : ""}>${l}</option>`,
              )
              .join("")}</select>
            <span class="muted small">${rs.length} 条</span></div>
            <section class="card" id="log-runs">${runsTable(rs, true, "log")}</section>`
          );
        }
        return (
          head +
          `<select data-logf="unit">${Object.keys((D.state && D.state.services) || {})   // 平台的服务：接口按 systemd 现找的列表，不写死
            .map((k) => [k, D.state.service_desc && D.state.service_desc[k] ? `${k}（${D.state.service_desc[k]}）` : k])
            .map(
              ([k, l]) =>
                `<option value="${esc(k)}" ${LOG.unit === k ? "selected" : ""}>${esc(l)}</option>`,
            )
            .join("")}</select>
          <span class="muted small">最近 300 行，5 秒刷新</span></div>
          <section class="card"><div class="journal" id="journal"><div class="muted">读取中…</div></div></section>`
        );
      }
      async function refreshJournal() {
        const box = $("#journal");
        if (!box) return;
        const j = await api(`/api/journal?unit=${LOG.unit}&n=300`).catch(
          (e) => ({ text: "读取失败：" + e.message }),
        );
        const atBottom =
          box.scrollTop + box.clientHeight >= box.scrollHeight - 20 ||
          !box.dataset.loaded;
        box.innerHTML = (j.text || "（无日志）")
          .split("\n")
          .filter(Boolean)
          .map((l) => {
            const m = l.match(/^(\S+)\s+\S+\s+(\S+?):\s?(.*)$/);
            const cls = /error|traceback|exception|失败|failed/i.test(l)
              ? "err"
              : /warn|警告/i.test(l)
                ? "warn"
                : "";
            if (!m) return `<div class="${cls}">${esc(l)}</div>`;
            const d = new Date(m[1]);
            return `<div class="${cls}"><span class="ts">${isNaN(d) ? esc(m[1]) : `${d.getMonth() + 1}/${d.getDate()} ${hm(d)}:${pad(d.getSeconds())}`}</span>  ${esc(m[3])}</div>`;
          })
          .join("");
        box.dataset.loaded = 1;
        if (atBottom) box.scrollTop = box.scrollHeight;
      }

      const isShell = (e) => e.tool === "shell" && e.args && typeof e.args.command === "string";

      // 自动刷新只更新会变的那几块，不动展开的折叠区与滚动位置
      function softRefresh() {
        const p = route();
        if ((p[0] || "tasks") === "tasks" && !p[1]) {
          if ($("#tl") && !document.querySelector(".menu:not([hidden])")) {
            const y = scrollY;
            $("#view").innerHTML = viewTasks();
            scrollTo(0, y);
          }
        } else if (p[0] === "tasks" && p[1]) {
          const box = $("#task-runs");
          if (box)
            box.innerHTML = runsTable(
              D.runs.filter((r) => r.task === p[1]),
              false,
              "task",
            );
        } else if (p[0] === "logs" && LOG.tab === "runs") {
          const box = $("#log-runs");
          if (box) box.innerHTML = runsTable(logRuns(), true, "log");
        } else if (p[0] === "apis") refreshApis();
      }

      document.addEventListener("click", async (e) => {
        const hb = e.target.closest("[data-histback]");
        if (hb) { e.preventDefault(); history.back(); return; }
        if (e.target.closest("[data-pendtoggle]")) { PEND_OPEN = !PEND_OPEN; renderNow(); return; }
        if (PEND_OPEN && !e.target.closest(".pend-pop")) { PEND_OPEN = false; renderNow(); }
        const km = e.target.closest("[data-menu]");
        // 菜单开着时，点菜单外面只负责收起菜单，不触发下面的跳转
        const openMenu = document.querySelector(".menu:not([hidden])");
        if (openMenu && !km && !openMenu.contains(e.target)) {
          openMenu.hidden = true; openMenu.previousElementSibling?.setAttribute("aria-expanded", "false");
          e.preventDefault(); return;
        }
        // 更改优先级：菜单里换成数字框，保存走和启用 / 禁用同一套 yaml 校验
        const pr = e.target.closest("[data-prio]");
        if (pr) {
          const m = pr.closest(".menu"), max = +pr.dataset.max;
          m.innerHTML = `<div class="prio-edit"><label>优先级（1–${max}，大的先跑）</label>
            <div class="prio-row"><input type="number" min="1" max="${max}" step="1" value="${esc(pr.dataset.cur)}" data-prioval /><button class="btn sm primary" data-priosave="${esc(pr.dataset.prio)}">保存</button></div></div>`;
          m.querySelector("[data-prioval]").select();
          return;
        }
        const ps = e.target.closest("[data-priosave]");
        if (ps) {
          ps.disabled = true;
          try {
            await api(`/api/tasks/${encodeURIComponent(ps.dataset.priosave)}/priority`, { method: "POST", body: JSON.stringify({ priority: ps.closest(".prio-edit").querySelector("[data-prioval]").value }) });
            toast("已更改优先级");
            D.tasks = await api("/api/tasks");
            render();
          } catch (err) { toast(err.message, true); ps.disabled = false; }
          return;
        }
        if (e.target.closest(".prio-edit")) return;     // 点数字框本身：不收菜单、不跳转
        if (e.target.closest("[data-schededit]")) return;   // 更改定时：由修改定时的弹框接手（不跳转）
        document.querySelectorAll(".menu").forEach((m) => {
          if (km && m.previousElementSibling === km) return;
          m.hidden = true; m.previousElementSibling?.setAttribute("aria-expanded", "false");
        });
        if (km) {
          const m = km.nextElementSibling; m.hidden = !m.hidden; km.setAttribute("aria-expanded", String(!m.hidden));
          m.classList.toggle("up", !m.hidden && km.getBoundingClientRect().bottom + m.offsetHeight + 12 > innerHeight);
          return;
        }
        const a = e.target.closest("[data-act]");
        if (a) {
          e.stopPropagation();
          if (a.dataset.confirm && !a.dataset.armed) {
            a.dataset.armed = 1;
            const old = a.textContent;
            a.textContent = a.dataset.confirm;
            a.classList.add("danger");
            setTimeout(() => {
              delete a.dataset.armed;
              a.textContent = old;
              a.classList.remove("danger");
            }, 3000);
            return;
          }
          a.disabled = true;
          try {
            await api(
              `/api/tasks/${encodeURIComponent(a.dataset.task)}/${a.dataset.act}`,
              { method: "POST", body: "{}" },
            );
            toast(
              {
                run: "已加入队列",
                enable: "已启用",
                disable: "已禁用",
                pause: "已请求暂停，将在下一个工具边界停下",
                resume: "已加入队列",
              }[a.dataset.act],
            );
            await load();
            render();
          } catch (err) {
            toast(err.message, true);
            a.disabled = false;
          }
          return;
        }
        const agp = e.target.closest("[data-agpage]");
        if (agp) {
          const n = +agp.dataset.agpage;
          AG.page = n ? AG.page + n : null;   // 0：回到默认那页
          $("#tl").innerHTML = timelineHTML();
          return;
        }
        const ts = e.target.closest("[data-tlstep]");
        if (ts) {
          const n = +ts.dataset.tlstep;
          TL_DAY = n ? TL_DAY + n : 0;
          $("#daynav").innerHTML = dayNavHTML();
          $("#tl").innerHTML = timelineHTML();
          return;
        }
        const ct = e.target.closest("[data-cattask]");
        if (ct) {
          CAT.task = ct.dataset.cattask;
          if (CAT.x && $("#cat")) $("#cat").innerHTML = catHTML(CAT.x);
          return;
        }
        const rt = e.target.closest("[data-runtab]");
        if (rt) {
          RUN_TAB = rt.dataset.runtab;
          document
            .querySelectorAll("[data-runtab]")
            .forEach((b) => b.classList.toggle("on", b === rt));
          const p = route();
          const d = RUN_CACHE[`${p[1]}/${p[2]}`];
          if (d) $("#run-body").innerHTML = runTab(d);
          return;
        }
        const lt = e.target.closest("[data-logtab]");
        if (lt) {
          LOG.tab = lt.dataset.logtab;
          render();
          return;
        }
        if (e.target.closest("[data-addopen]")) {
          const b = $("#tool-add"), o = e.target.closest("[data-addopen]"), kind = o.dataset.addopen || "tools";
          const swap = !b.hidden && ADD.kind !== kind;      // 开着另一种的面板：换成这一种，不收起
          Object.assign(ADD, { tool: "", err: "", q: "", kind, task: o.dataset.task || "" }); redrawAdd(); b.hidden = swap ? false : !b.hidden; if (!b.hidden) $("[data-addsearch]")?.focus(); return;
        }
        const pk = e.target.closest("[data-pick]");
        if (pk) { Object.assign(ADD, { tool: pk.dataset.pick, err: "", q: "" }); redrawAdd(); return; }
        if (e.target.closest("[data-addclose]")) { $("#tool-add").hidden = true; return; }
        const ag = e.target.closest("[data-addgo]");
        if (ag) {
          ag.disabled = true;
          try {
            const conf = ADD.kind === "subagents" ? (taskByName(ag.dataset.task)?.removed_subagents || {})[ADD.tool] : null;   // 撤掉时记下的选项（guide…）原样加回
            await api(`/api/tasks/${encodeURIComponent(ag.dataset.task)}/${ADD.kind}/add`, { method: "POST", body: JSON.stringify({ name: ADD.tool, yaml: conf && Object.keys(conf).length ? toYaml(conf).trim() : "" }) });
            toast(D.state?.current?.task === ag.dataset.task ? `已添加 ${ADD.tool}，下次开跑生效` : `已添加 ${ADD.tool}`);
            Object.assign(ADD, { tool: "", err: "", q: "" });
            $("#tool-add").hidden = true;                 // 加好了就收起搜索框
            D.tasks = await api("/api/tasks"); toolsChanged();
          } catch (err) { ADD.err = err.message; redrawAdd(); }
          return;
        }
        const tr = e.target.closest("[data-toolrm]");
        if (tr) {
          if (!tr.dataset.armed) {
            tr.dataset.armed = 1; tr.textContent = "确认撤销"; tr.classList.add("danger");
            setTimeout(() => { delete tr.dataset.armed; tr.textContent = "撤销"; tr.classList.remove("danger"); }, 3000);
            return;
          }
          tr.disabled = true;
          try {
            const kind = tr.dataset.kind || "tools";
            await api(`/api/tasks/${encodeURIComponent(tr.dataset.task)}/${kind}/remove`, { method: "POST", body: JSON.stringify({ name: tr.dataset.toolrm }) });
            toast(D.state?.current?.task === tr.dataset.task ? `已撤销 ${tr.dataset.toolrm}，下一次${kind === "subagents" ? "" : "工具"}调用起生效` : `已撤销 ${tr.dataset.toolrm}`);
            D.tasks = await api("/api/tasks"); toolsChanged();
          } catch (err) { toast(err.message, true); tr.disabled = false; }
          return;
        }
        const mb = e.target.closest("[data-more]");
        if (mb) {
          RUN_LIMIT[mb.dataset.more] = (RUN_LIMIT[mb.dataset.more] || 30) + 30;
          softRefresh();
          return;
        }
        const row = e.target.closest("[data-href]");
        if (row && !e.target.closest("a,button"))
          location.hash = row.dataset.href;
      });
      document.addEventListener("keydown", (e) => {   // 优先级数字框里回车 = 保存
        if (e.key === "Enter" && e.target.matches?.("[data-prioval]")) e.target.closest(".prio-edit").querySelector("[data-priosave]").click();
      });
      document.addEventListener("input", (e) => {
        if (!e.target.matches?.("[data-addsearch]")) return;
        ADD.q = e.target.value;
        const t = addTask(), box = $("#picklist");
        if (t && box) { box.innerHTML = pickList(addCands(t)); box.hidden = !!ADD.tool && !ADD.q.trim(); }
      });
      // Skills 页的导入：选的文件（选文件夹时带相对路径）读成 base64 交给 /api/skills/import，装好跳到它的详情页
      async function skillUpload(input) {
        const list = [...input.files];
        input.value = "";
        if (!list.length) return;
        if (list.reduce((n, f) => n + f.size, 0) > 20 * 1024 * 1024) return toast("超过 20 MB", true);
        toast("正在导入…");
        try {
          const files = await Promise.all(list.map((f) => new Promise((ok, bad) => {
            const r = new FileReader();
            r.onload = () => ok({ path: f.webkitRelativePath || f.name, data: String(r.result).split(",")[1] || "" });
            r.onerror = bad;
            r.readAsDataURL(f);
          })));
          const r = await api("/api/skills/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ files }) });
          toast(`已导入 skill ${r.name}`);
          location.hash = `#/skills/${encodeURIComponent(r.name)}`;
        } catch (e) {
          toast(e.message, true);
        }
      }
      document.addEventListener("change", (e) => {
        if (e.target.matches?.("[data-skillup]")) return skillUpload(e.target);
        const f = e.target.dataset?.logf;
        if (!f) return;
        LOG[f] = e.target.value;
        if (f === "unit") {
          const b = $("#journal");
          if (b) {
            delete b.dataset.loaded;
            refreshJournal();
          }
        } else render();
      });
      let toastT;
      function toast(msg, bad) {
        const t = $("#toast");
        t.textContent = msg;
        t.className = "toast show" + (bad ? " bad" : "");
        clearTimeout(toastT);
        toastT = setTimeout(
          () => (t.className = "toast" + (bad ? " bad" : "")),
          3200,
        );
      }

      // ---------- 刷新 ----------
      async function loadState() {
        const s = await poll("/api/state");
        if (!s) return;
        D.state = s;
        renderNow();
      }
      async function load() {
        const [s, t, r, tl, sa] = await Promise.all([
          api("/api/state"),
          api("/api/tasks"),
          api("/api/runs?limit=1000"),
          api("/api/tools"),
          api("/api/subagents"),
        ]);
        Object.assign(D, {
          state: s,
          tasks: t,
          runs: r,
          tools: tl,
          subagents: sa,
          loaded: true,
        });
        computeAvg();
        renderNow();
      }
      setInterval(() => loadState().catch(() => {}), 3000);
      setInterval(async () => {
        try {
          const prev = D.state?.current?.run_id;
          const runs = await poll("/api/runs?limit=1000");
          if (!runs) return;
          D.runs = runs;
          computeAvg();
          if (Math.random() < 0.34 || prev !== D.state?.current?.run_id) {
            const [t, tl] = await Promise.all([poll("/api/tasks"), poll("/api/tools")]);
            if (!t || !tl) return;
            [D.tasks, D.tools] = [t, tl];
          }
          renderNow();
          softRefresh();
        } catch {}
      }, 15000);
      setInterval(() => {
        if (route()[0] === "logs" && LOG.tab === "journal") refreshJournal();
      }, 5000);
