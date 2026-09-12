/**
 * Capability-map coverage against Python class/function definitions.
 *
 * Browser: load beside index.html with definitions.json (or pass defs).
 * Node CLI (from repo root or this folder):
 *   node coverage.js
 *   node coverage.js --write-definitions
 *   node coverage.js --json
 *
 * Coverage = definitions referenced by map code refs ÷ all class/def
 * symbols under src/cursor_spend_tray (nested helpers inside functions skipped).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CapabilityCoverage = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function walkFeatures(nodes, refs, parentId) {
    for (var i = 0; i < (nodes || []).length; i++) {
      var node = nodes[i];
      var fid = String(node.id || parentId || "?");
      var code = node.code || [];
      for (var j = 0; j < code.length; j++) {
        var raw = code[j];
        if (!raw || !raw.file) continue;
        refs.push({
          file: String(raw.file).replace(/\\/g, "/"),
          function: raw.function != null ? String(raw.function) : null,
          line: raw.line != null ? Number(raw.line) : null,
          feature_id: fid,
        });
      }
      walkFeatures(node.children || [], refs, fid);
    }
  }

  function collectRefs(mapData) {
    var refs = [];
    walkFeatures((mapData && mapData.features) || [], refs, "");
    return refs;
  }

  /**
   * Extract class / top-level function / method definitions from Python text.
   * Skips nested functions inside other functions (same rule as the former
   * Python helper). Indent-based; good enough for this codebase.
   */
  function extractDefinitions(fileRel, source) {
    var lines = String(source).split(/\r?\n/);
    var defs = [];
    var classStack = []; // { name, indent }
    var skipUntilIndent = null;

    function effectiveIndent(line) {
      var m = /^[ \t]*/.exec(line);
      var ws = m ? m[0] : "";
      // tabs as 8 spaces — matches common Python display width enough for us
      return ws.replace(/\t/g, "        ").length;
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var trimmed = line.trim();
      if (!trimmed || trimmed.charAt(0) === "#") continue;

      var indent = effectiveIndent(line);

      if (skipUntilIndent != null) {
        if (indent > skipUntilIndent) continue;
        skipUntilIndent = null;
      }

      while (classStack.length && indent <= classStack[classStack.length - 1].indent) {
        classStack.pop();
      }

      var classMatch = /^(class)\s+([A-Za-z_][A-Za-z0-9_]*)\b/.exec(trimmed);
      if (classMatch) {
        defs.push({
          file: fileRel,
          name: classMatch[2],
          line: i + 1,
          kind: "class",
        });
        classStack.push({ name: classMatch[2], indent: indent });
        continue;
      }

      var fnMatch = /^(async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b/.exec(trimmed);
      if (fnMatch) {
        var name = fnMatch[2];
        if (classStack.length) {
          var parts = classStack.map(function (c) {
            return c.name;
          });
          parts.push(name);
          defs.push({
            file: fileRel,
            name: parts.join("."),
            line: i + 1,
            kind: "method",
          });
        } else {
          defs.push({
            file: fileRel,
            name: name,
            line: i + 1,
            kind: "function",
          });
        }
        // Skip nested defs inside this function body.
        skipUntilIndent = indent;
      }
    }
    return defs;
  }

  function normalizeDefinitions(definitions) {
    return (definitions || []).map(function (d) {
      return {
        file: String(d.file).replace(/\\/g, "/"),
        name: String(d.name),
        line: Number(d.line),
        kind: d.kind || "function",
      };
    });
  }

  function matchCoverage(defs, refs) {
    var byName = Object.create(null);
    var byLine = Object.create(null);
    var keyOf = function (d) {
      return d.file + "::" + d.name + "@" + d.line;
    };

    for (var i = 0; i < defs.length; i++) {
      var d = defs[i];
      var nk = d.file + "\0" + d.name;
      if (!byName[nk]) byName[nk] = [];
      byName[nk].push(d);
      byLine[d.file + "\0" + d.line] = d;
    }

    var coveredMap = Object.create(null);
    var unmatched = [];
    var matched = [];

    for (var r = 0; r < refs.length; r++) {
      var ref = refs[r];
      var hit = null;
      if (ref.function) {
        var candidates = byName[ref.file + "\0" + ref.function] || [];
        if (candidates.length === 1) {
          hit = candidates[0];
        } else if (candidates.length && ref.line != null) {
          for (var c = 0; c < candidates.length; c++) {
            if (candidates[c].line === ref.line) {
              hit = candidates[c];
              break;
            }
          }
          if (!hit) hit = candidates[0];
        } else if (candidates.length) {
          hit = candidates[0];
        }
      }
      if (!hit && ref.line != null) {
        hit = byLine[ref.file + "\0" + ref.line] || null;
      }
      if (!hit) {
        unmatched.push(ref);
      } else {
        coveredMap[keyOf(hit)] = hit;
        matched.push(ref);
      }
    }

    var covered = Object.keys(coveredMap).map(function (k) {
      return coveredMap[k];
    });
    return { covered: covered, unmatched: unmatched, matched: matched };
  }

  function computeCoverage(mapData, definitionsInput) {
    var defs = normalizeDefinitions(
      Array.isArray(definitionsInput)
        ? definitionsInput
        : (definitionsInput && definitionsInput.definitions) || []
    );
    var refs = collectRefs(mapData || {});
    var result = matchCoverage(defs, refs);
    var total = defs.length;
    var coveredN = result.covered.length;
    var pct = total ? (100 * coveredN) / total : 0;

    var uniqueLines = Object.create(null);
    var uniqueFns = Object.create(null);
    for (var i = 0; i < refs.length; i++) {
      var ref = refs[i];
      if (ref.line != null) uniqueLines[ref.file + ":" + ref.line] = true;
      if (ref.function) uniqueFns[ref.file + ":" + ref.function] = true;
    }

    function kindStats(kind) {
      var t = 0;
      var c = 0;
      var coveredSet = Object.create(null);
      for (var j = 0; j < result.covered.length; j++) {
        coveredSet[
          result.covered[j].file +
            "::" +
            result.covered[j].name +
            "@" +
            result.covered[j].line
        ] = true;
      }
      for (var k = 0; k < defs.length; k++) {
        if (defs[k].kind !== kind) continue;
        t++;
        var key =
          defs[k].file + "::" + defs[k].name + "@" + defs[k].line;
        if (coveredSet[key]) c++;
      }
      return { total: t, covered: c };
    }

    return {
      definitions_total: total,
      definitions_covered: coveredN,
      coverage_percent: Math.round(pct * 100) / 100,
      code_refs_total: refs.length,
      code_refs_matched: result.matched.length,
      code_refs_unmatched: result.unmatched.length,
      unique_ref_file_lines: Object.keys(uniqueLines).length,
      unique_ref_functions: Object.keys(uniqueFns).length,
      by_kind: {
        class: kindStats("class"),
        function: kindStats("function"),
        method: kindStats("method"),
      },
      unmatched_refs: result.unmatched,
      covered: result.covered,
      definitions: defs,
      refs: refs,
    };
  }

  function formatCoverageLine(report) {
    if (!report) return "Coverage unavailable";
    var pct = report.coverage_percent.toFixed(1);
    return (
      "Definition coverage: " +
      report.definitions_covered +
      "/" +
      report.definitions_total +
      " (" +
      pct +
      "%) · " +
      report.code_refs_matched +
      "/" +
      report.code_refs_total +
      " code refs matched"
    );
  }

  return {
    collectRefs: collectRefs,
    extractDefinitions: extractDefinitions,
    computeCoverage: computeCoverage,
    formatCoverageLine: formatCoverageLine,
  };
});

// Node CLI — keep vars inside an IIFE. Top-level `var` in this file is still
// hoisted onto `window` when the browser loads coverage.js as a classic script
// (even when this `if` is false), which breaks index.html's `let definitionsDoc`.
if (typeof require !== "undefined" && typeof module !== "undefined" && require.main === module) {
  (function () {
  var fs = require("fs");
  var path = require("path");
  var api = module.exports;

  var here = __dirname;
  var assetsDir = here;
  // scripts/ → assets/ when placed under scripts/; also support living in assets/
  if (path.basename(here) === "scripts") {
    assetsDir = path.join(here, "..", "assets");
  }
  var skillDir = path.basename(assetsDir) === "assets" ? path.dirname(assetsDir) : here;
  var repoRoot = path.resolve(skillDir, "..", "..", "..");
  var mapPath = path.join(assetsDir, "capability-map.json");
  var defsPath = path.join(assetsDir, "definitions.json");
  var srcDir = path.join(repoRoot, "src", "cursor_spend_tray");

  var args = process.argv.slice(2);
  var asJson = args.indexOf("--json") !== -1;
  var writeDefs = args.indexOf("--write-definitions") !== -1;
  var showUnmatched = args.indexOf("--show-unmatched") !== -1;

  function listPyFiles(dir) {
    var out = [];
    function walk(d) {
      fs.readdirSync(d, { withFileTypes: true }).forEach(function (ent) {
        var p = path.join(d, ent.name);
        if (ent.isDirectory()) {
          if (ent.name === "__pycache__") return;
          walk(p);
        } else if (ent.isFile() && ent.name.endsWith(".py")) {
          out.push(p);
        }
      });
    }
    walk(dir);
    return out.sort();
  }

  function buildDefinitions() {
    var defs = [];
    listPyFiles(srcDir).forEach(function (abs) {
      var rel = path.relative(repoRoot, abs).split(path.sep).join("/");
      var text = fs.readFileSync(abs, "utf8");
      defs = defs.concat(api.extractDefinitions(rel, text));
    });
    return {
      generated_from: "src/cursor_spend_tray",
      definitions: defs,
    };
  }

  var definitionsDoc;
  if (writeDefs || !fs.existsSync(defsPath)) {
    definitionsDoc = buildDefinitions();
    if (writeDefs) {
      fs.writeFileSync(defsPath, JSON.stringify(definitionsDoc, null, 2) + "\n", "utf8");
      process.stderr.write("Wrote " + path.relative(repoRoot, defsPath) + "\n");
    }
  } else {
    definitionsDoc = JSON.parse(fs.readFileSync(defsPath, "utf8"));
  }

  var mapData = JSON.parse(fs.readFileSync(mapPath, "utf8"));
  var report = api.computeCoverage(mapData, definitionsDoc);

  if (asJson) {
    var out = {
      map: path.relative(repoRoot, mapPath).split(path.sep).join("/"),
      definitions: path.relative(repoRoot, defsPath).split(path.sep).join("/"),
      meta_version: (mapData.meta || {}).version,
      definitions_total: report.definitions_total,
      definitions_covered: report.definitions_covered,
      coverage_percent: report.coverage_percent,
      code_refs_total: report.code_refs_total,
      code_refs_matched: report.code_refs_matched,
      code_refs_unmatched: report.code_refs_unmatched,
      unique_ref_file_lines: report.unique_ref_file_lines,
      unique_ref_functions: report.unique_ref_functions,
      by_kind: report.by_kind,
    };
    if (showUnmatched) out.unmatched_refs = report.unmatched_refs;
    process.stdout.write(JSON.stringify(out, null, 2) + "\n");
  } else {
    process.stdout.write("Capability map → definition coverage\n");
    process.stdout.write("  " + api.formatCoverageLine(report) + "\n");
    Object.keys(report.by_kind).forEach(function (kind) {
      var s = report.by_kind[kind];
      var kpct = s.total ? ((100 * s.covered) / s.total).toFixed(1) : "0.0";
      process.stdout.write(
        "    " + kind.padEnd(8) + " " + s.covered + "/" + s.total + " (" + kpct + "%)\n"
      );
    });
    if (showUnmatched && report.unmatched_refs.length) {
      process.stdout.write("\nUnmatched code refs:\n");
      report.unmatched_refs.forEach(function (r) {
        process.stdout.write(
          "  - " +
            r.file +
            ":" +
            (r.line != null ? r.line : "?") +
            " " +
            (r.function || "(no function)") +
            " [" +
            r.feature_id +
            "]\n"
        );
      });
    }
  }
  })();
}
