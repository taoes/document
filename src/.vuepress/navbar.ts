import { navbar } from "vuepress-theme-hope";

export default navbar([
  "/",
  {
    text: "博文",
    icon: "pen-to-square",
    prefix: "/",
    children: [
      { text: "全部文章", icon: "list", link: "/article/" },
      { text: "Java", icon: "code", link: "/category/java/" },
      { text: "Python", icon: "terminal", link: "/category/python/" },
      { text: "Agent 实践", icon: "robot", link: "/category/agent/" },
    ],
  },
  {
    text: "分类",
    icon: "folder-open",
    link: "/category/",
  },
  {
    text: "标签",
    icon: "tag",
    link: "/tag/",
  },
  {
    text: "时间轴",
    icon: "clock",
    link: "/timeline/",
  },
  {
    text: "关于",
    icon: "circle-info",
    link: "/intro.html",
  },
]);
