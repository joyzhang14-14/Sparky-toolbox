# obsidian 用法

查笔记：obsidian_find → obsidian_info → obsidian_read。
- obsidian_find(keyword)：文件名或正文含 keyword（不分大小写）的 .md，返回库内相对路径；最多 50 条，truncated=true 表示还有（换更具体的词）。
- obsidian_info(path)：标题（第一个一级标题，没有就用文件名）、字符数 chars、创建 / 修改时间、frontmatter 原文、标题列表。按 chars 决定读多少。
- obsidian_read(path, chars, start?)：path 是库内相对路径（如 UCI/UCI 新生待办清单.md）；返回从 start（默认 0）起 chars 个字符 text 与全文字符数 total；长笔记按 start 分段读。

写笔记 obsidian_write(path, content, reason)：
- content 是写入后的全文；reason 一句话说明为什么改（给 Joy 看）。
- 改已有笔记：先 obsidian_read 读全文，只改必要的行。
- Joy 拒绝并写了意见就照着改完再提议，不要改了内容重复提议。
- USER/memory.md 用 memory_edit，USER/USER.md 用 memory_user_edit；. 开头的目录不能写；不能删文件。
