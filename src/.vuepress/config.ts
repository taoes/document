import { defineUserConfig } from "vuepress";

import theme from "./theme.js";

export default defineUserConfig({
  base: "/",

  lang: "zh-CN",
  title: "此间余序",
  description: "记录 Java、Python 与 Agent 实践的深度技术博客",

  theme,

  // 与 PWA 一起启用
  shouldPrefetch: false,
});
