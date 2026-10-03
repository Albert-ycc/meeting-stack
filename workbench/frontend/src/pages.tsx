import { lazyPage } from "./lazyPage";

// 路由页各自一块代码，点到才用得上，顺序照 App 原来 import 的顺序。样式文件不跟着页面走，加载顺序见 pageStyles.ts。
// 工作台（落地页）、需求池和需求新增页（App 还要从里面取常量）、两个抽屉留在入口里
export const GlossaryPage = lazyPage(() => import("./components/GlossaryPage").then((m) => m.GlossaryPage));
export const JobsPage = lazyPage(() => import("./components/JobsPage").then((m) => m.JobsPage));
export const LibraryPage = lazyPage(() => import("./components/LibraryPage").then((m) => m.LibraryPage));
export const MeetingDetailPage = lazyPage(() =>
  import("./components/MeetingDetailPage").then((m) => m.MeetingDetailPage),
);
export const ProjectDetailPage = lazyPage(() =>
  import("./components/ProjectDetailPage").then((m) => m.ProjectDetailPage),
);
export const OverviewGraph = lazyPage(() => import("./components/graph/OverviewGraph").then((m) => m.OverviewGraph));
export const ProjectGraph = lazyPage(() => import("./components/graph/ProjectGraph").then((m) => m.ProjectGraph));
export const ProjectsPage = lazyPage(() => import("./components/ProjectsPage").then((m) => m.ProjectsPage));
export const RequirementDetailPage = lazyPage(() =>
  import("./components/RequirementDetailPage").then((m) => m.RequirementDetailPage),
);
export const RequirementsPage = lazyPage(() => import("./components/RequirementsPage").then((m) => m.RequirementsPage));
export const SearchPage = lazyPage(() => import("./components/SearchPage").then((m) => m.SearchPage));
export const TasksPage = lazyPage(() => import("./components/TasksPage").then((m) => m.TasksPage));

const pages = [
  GlossaryPage,
  JobsPage,
  LibraryPage,
  MeetingDetailPage,
  ProjectDetailPage,
  OverviewGraph,
  ProjectGraph,
  ProjectsPage,
  RequirementDetailPage,
  RequirementsPage,
  SearchPage,
  TasksPage,
];

/** 第一屏出来以后调：在后台把所有页的代码拉下来。哪块没拉到不用管，点到那一页时会再试一次 */
export async function preloadPages() {
  await Promise.allSettled(pages.map((page) => page.preload()));
}
