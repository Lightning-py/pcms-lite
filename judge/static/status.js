"use strict";
const statusElement = document.getElementById("submission-status");
if (statusElement && ["QUEUED", "RUNNING"].includes(statusElement.dataset.verdict)) {
  const poll = async () => {
    try {
      const response = await fetch(`/api/submissions/${statusElement.dataset.id}`, {headers: {Accept: "application/json"}});
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("session");
      const result = await response.json();
      if (!["QUEUED", "RUNNING"].includes(result.verdict)) { location.reload(); return; }
      const verdict = document.getElementById("verdict");
      verdict.textContent = result.verdict;
      verdict.className = `verdict ${result.verdict}`;
      document.getElementById("test-number").textContent = result.test_number ?? "—";
      document.getElementById("time-ms").textContent = result.time_ms ?? "—";
      document.getElementById("memory-kb").textContent = result.memory_kb ?? "—";
      document.getElementById("poll-message").textContent = "Статус обновляется автоматически.";
      setTimeout(poll, 2000);
    } catch {
      document.getElementById("poll-message").textContent = "Не удалось обновить статус. Проверьте соединение или обновите страницу.";
      setTimeout(poll, 10000);
    }
  };
  setTimeout(poll, 1500);
}
