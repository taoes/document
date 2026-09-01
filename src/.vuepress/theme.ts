import { hopeTheme } from "vuepress-theme-hope";

import navbar from "./navbar.js";
import sidebar from "./sidebar.js";

export default hopeTheme({
  hostname: "https://cijian-yuxu.example.com",

  author: {
    name: "此间余序",
  },

  docsDir: "src",

  // 导航栏
  navbar,

  // 侧边栏
  sidebar,

  // 页脚
  footer: "此间余序 · 记录技术的秩序与余韵",
  displayFooter: true,

  // 博客相关
  blog: {
    description: "Java · Python · Agent 实践",
    intro: "/intro.html",
  },

  metaLocales: {
    editLink: "在 GitHub 上编辑此页",
  },

  // 此处开启常用的 Markdown 增强功能
  markdown: {
    align: true,
    attrs: true,
    codeTabs: true,
    component: true,
    figure: true,
    gfm: true,
    imgLazyload: true,
    imgSize: true,
    mark: true,
    spoiler: true,
    sub: true,
    sup: true,
    tabs: true,
    tasklist: true,
    vPre: true,
  },

  // 在这里配置主题提供的插件
  plugins: {
    blog: true,

    components: {
      components: ["Badge", "VPCard"],
    },

    icon: {
      prefix: "fa6-solid:",
    },
  },
});
