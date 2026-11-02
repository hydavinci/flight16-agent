export const statuses = {
  queued: "排队中",
  planning: "正在规划",
  waiting_for_user: "等待确认或补充",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

export const nodeStatuses = {
  pending: "待执行",
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

export function createState() {
  return {
    token: "",
    current: null,
    pendingView: null,
    selection: 0,
    sending: false,
    timer: null,
    baselineKey: "",
    historyRequest: 0,
    directoryPicker: null,
    directoryRequest: 0,
    nativeDirectoryPickerEnabled: false,
    nativePicker: null,
    locations: [],
  };
}

export function active(state) {
  return state.pendingView !== null
    || Boolean(
      state.current
      && ["queued", "planning", "running"].includes(state.current.status),
    );
}
