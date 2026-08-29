import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    testTimeout: 20000,
    // The integration/lifecycle tests drive one real Notepad instance at a
    // time — parallel workers would race each other for the same window.
    fileParallelism: false,
  },
});
