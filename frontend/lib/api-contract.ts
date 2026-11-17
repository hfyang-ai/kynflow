/**
 * 前后端类型契约校验。
 *
 * `lib/api-schema.d.ts` 由 `npm run gen:api-types` 从 FastAPI 的 OpenAPI schema 生成。
 * 这里用「后端类型必须可赋值给前端手写类型」的方式做编译期断言：
 * 如果后端删改字段而前端没跟上，`tsc` 会直接报错，而不是等到运行时才发现。
 *
 * 典型工作流：
 *   1. 后端改了 schema
 *   2. npm run gen:api-types
 *   3. npm run typecheck —— 报错处即需要同步的前端类型
 */
import type { components } from "./api-schema";

import type { Issue, PullRequest, WorkflowRun } from "@/app/home/types";

type Schemas = components["schemas"];

type BackendIssue = Schemas["IssueResponse"];
type BackendPullRequest = Schemas["PullRequestResponse"];
type BackendWorkflowRun = Schemas["WorkflowRunResponse"];

/** 断言 A 可以被当作 B 使用（A 省略字段是允许的，改字段类型/删字段会失败）。 */
type Compatible<Actual, Expected> = Actual extends Expected ? true : false;

/** 只有 Compatible 结果为 true 才能通过，否则 tsc 报 “false 不满足 true”。 */
type Assert<T extends true> = T;

export type IssueContract = Assert<Compatible<BackendIssue, Issue>>;
export type PullRequestContract = Assert<Compatible<BackendPullRequest, PullRequest>>;
export type WorkflowRunContract = Assert<Compatible<BackendWorkflowRun, WorkflowRun>>;

// 列表接口的轻量类型别名，供需要精确对齐后端时使用。
export type PullRequestSummary = Schemas["PullRequestSummaryResponse"];
export type WorkflowRunSummary = Schemas["WorkflowRunSummaryResponse"];
