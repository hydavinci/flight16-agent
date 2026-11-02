export const API = "/api/conversations";

export function createApi(getToken) {
  async function response(path, options = {}) {
    const headers = {"X-Flight-Token": getToken()};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const result = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    if (!result.ok) {
      let detail;
      try {
        const data = await result.json();
        detail = typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail || data);
      } catch {
        detail = `HTTP ${result.status}`;
      }
      throw new Error(detail);
    }
    return result;
  }

  async function api(path, options) {
    return (await response(path, options)).json();
  }

  return {response, api};
}
