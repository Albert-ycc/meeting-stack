import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";

describe("OverviewPage mobile safety", () => {
  it("renders task metrics as static information on mobile", () => {
    const onOpenJobs = vi.fn();
    render(
      <OverviewPage
        health={{
          status: "ok",
          services: {},
          counts: { meetings: 1, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
        }}
        jobs={[]}
        jobsAvailable
        jobsInteractive={false}
        meetings={[]}
        onOpenJobs={onOpenJobs}
        onOpenLibrary={vi.fn()}
      />,
    );

    expect(screen.getByText("需要处理").closest("button")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看全部" })).not.toBeInTheDocument();
    expect(onOpenJobs).not.toHaveBeenCalled();
  });

  it("summarises recent meetings by recording date", () => {
    render(
      <OverviewPage
        health={{
          status: "ok",
          services: { database: "healthy", semantic: "ready" },
          counts: { meetings: 2, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
        }}
        jobs={[]}
        jobsAvailable
        meetings={[
          {
            id: "vm-1",
            title: "协会MDT需求评审",
            recording_date: new Date().toISOString(),
            duration_ms: 3_600_000,
            status: "completed_unreviewed",
            tags: [],
          },
          {
            id: "vm-2",
            title: "vm-2",
            recording_date: new Date().toISOString(),
            duration_ms: 600_000,
            status: "completed_unreviewed",
            tags: [],
          },
        ]}
        onOpenJobs={vi.fn()}
        onOpenLibrary={vi.fn()}
        onOpenMeeting={vi.fn()}
      />,
    );

    expect(screen.getByText("本周录音")).toBeInTheDocument();
    expect(screen.getByText("本地服务全部正常")).toBeInTheDocument();
    expect(screen.getByText("协会MDT需求评审")).toBeInTheDocument();
    expect(screen.getAllByText("标题待生成").length).toBeGreaterThan(0);
  });
});
