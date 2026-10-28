# modify-calendar 用法

时间一律用本机时区。
- modify-calendar_list(start, end, calendar)：start / end 写 YYYY-MM-DD（含首不含尾；end 不填 = start 后 7 天）；calendar 不填查全部日历。本工具建的事件标「本工具创建」，带 uid。
- modify-calendar_add(title, start, calendar, end, location, notes, alarm_minutes)：
  - start / end 写 "YYYY-MM-DD HH:MM"；全天事件只写 YYYY-MM-DD，end 可不填；带时间的 end 不填 = 1 小时。start、end 同为全天或同带时间，end 晚于 start。
  - calendar 必填，日历名（如 School），放哪个按任务的规则；alarm_minutes 提前几分钟提醒，不填不提醒。
  - 加之前先 list 查同一天有没有相同事件，别重复加。
- modify-calendar_delete(uid, calendar)：uid 从标「本工具创建」的行拿；calendar 写它所在的日历；只找前 90 天到后 400 天。

时间只用 Joy 或原文给的，不自己编：
- 只有开始时间：建 1 小时的事件，notes 写「结束时间未知」。
- 随时可去的时间窗（如「9/28–9/29 12:00–16:00 可领」）按原文整段写（跨天或没有具体时段的写全天），不自己挑时段。

要先报名 / 申请才能参加的活动不进日历，告诉 Joy 要先报名。
