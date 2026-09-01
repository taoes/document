import { sidebar } from "vuepress-theme-hope";

export default sidebar({
  "/": [
    "",
    "intro",
    {
      text: "Java",
      icon: "code",
      prefix: "posts/java/",
      children: "structure",
    },
    {
      text: "Python",
      icon: "terminal",
      prefix: "posts/python/",
      children: "structure",
    },
    {
      text: "Agent 实践",
      icon: "robot",
      prefix: "posts/agent/",
      children: "structure",
    },
  ],
});
