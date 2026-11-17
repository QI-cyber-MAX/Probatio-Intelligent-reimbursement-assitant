# 字体使用声明

Probatio 界面使用 MiSans 字体；英文品牌字标使用 Manrope V5 by Mikhail Sharanda。

| 字体 | 本地文件 | 来源与许可 |
|---|---|---|
| MiSans Regular / Medium | `MiSans-Regular.woff2`、`MiSans-Medium.woff2` | [小米官方发布页](https://hyperos.mi.com/font/zh/download/)，官方 `MiSans.zip` 内同名原始文件；完整许可见 [MiSans-LICENSE.pdf](./MiSans-LICENSE.pdf) |
| Manrope V5 Regular / Medium | `ManropeV5-Regular.woff2`、`ManropeV5-Medium.woff2` | [作者发布页](https://www.sharanda.com/manrope)，官方 `manrope.zip` 的 `webfonts/` 原始文件；完整许可见 [Manrope-LICENSE.txt](./Manrope-LICENSE.txt) |

保留原始字体文件，未作子集化、字形修改或格式转换。页面通过 `@font-face` 加载 400 / 500 实际字重，`font-display: swap`，禁止合成字重；MiSans 加载失败时按系统中文字体回退。此目录的字体随 Probatio 界面使用。
