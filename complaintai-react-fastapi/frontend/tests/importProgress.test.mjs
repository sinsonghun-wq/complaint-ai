import test from "node:test";
import assert from "node:assert/strict";
import { importPercentage } from "../src/importProgress.js";

test("preparation and empty totals are zero, not NaN", () => {
  assert.equal(importPercentage(null), 0);
  assert.equal(importPercentage({ total: 0, completed: 0 }), 0);
});
test("actual CSV and document counts determine progress", () => {
  assert.equal(importPercentage({ total: 1297, completed: 10 }), 0);
  assert.equal(importPercentage({ total: 1200, completed: 500 }), 41);
  assert.equal(importPercentage({ total: 1200, completed: 1000 }), 83);
});
test("1200 records advance one percent per twelve records", () => {
  assert.equal(importPercentage({ total: 1200, completed: 11 }), 0);
  assert.equal(importPercentage({ total: 1200, completed: 12 }), 1);
  assert.equal(importPercentage({ total: 1200, completed: 24 }), 2);
  assert.equal(importPercentage({ total: 1200, completed: 600 }), 50);
  assert.equal(importPercentage({ total: 1200, completed: 1199 }), 99);
});
test("only successful completion displays 100 percent", () => {
  assert.equal(importPercentage({ total: 10, completed: 10, status: "running" }), 99);
  assert.equal(importPercentage({ total: 10, completed: 10, status: "completed" }), 100);
  assert.equal(importPercentage({ total: 10, completed: -2, status: "failed" }), 0);
});
