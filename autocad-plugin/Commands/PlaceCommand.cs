using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using LightingAI.Models;
using LightingAI.Services;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace LightingAI.Commands
{
    /// <summary>
    /// LIGHTINGAI_PLACE — main command
    ///
    /// Workflow:
    ///   1. Verify the Python backend is reachable
    ///   2. Prompt for project metadata (project name, customer, concept)
    ///   3. Save current DWG to a temp file (preserves all original geometry)
    ///   4. Upload to POST /process → receive job_id
    ///   5. Poll GET /jobs/{id} until status == "done"
    ///   6. For each PlacedLuminaire in the response:
    ///        a. Ensure the MIKA80-E-{type} block definition exists
    ///        b. INSERT the block at (x, y) with ATTRIBs attached
    ///   7. Draw the legend (model space, right of drawing)
    ///   8. Draw the title block (model space, below drawing)
    ///
    /// The command is idempotent — running it again removes all AI-* entities
    /// before re-placing, so the designer can tweak settings and re-run.
    /// </summary>
    public class PlaceCommand
    {
        private static readonly Dictionary<string, short> TypeAci = new()
        {
            { "A", 6 }, { "B", 1 }, { "C", 4 }, { "D", 2 }, { "E", 5 },
        };

        // ── Entry point ───────────────────────────────────────────────────────
        [CommandMethod("LIGHTINGAI_PLACE", CommandFlags.Modal)]
        public void Run()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            var ed  = doc.Editor;
            var db  = doc.Database;

            // ── 1. Health check ───────────────────────────────────────────────
            ed.WriteMessage($"\n[LightingAI] Connecting to {ApiClient.BaseUrl} …\n");
            if (!ApiClient.CheckHealth())
            {
                ed.WriteMessage($"[LightingAI] ✗  Cannot reach backend at {ApiClient.BaseUrl}\n");
                ed.WriteMessage("[LightingAI]    Start the API:  uvicorn services.api.main:app --port 8000\n");
                ed.WriteMessage("[LightingAI]    Change URL:     LIGHTINGAI_SETTINGS\n");
                return;
            }
            ed.WriteMessage("[LightingAI] ✓  Backend online.\n");

            // ── 2. Project metadata ───────────────────────────────────────────
            string projectName = Prompt(ed, "Project name",  "Rossmann EG", allowSpaces: true);
            string customer    = Prompt(ed, "Customer",       "Dirk Rossmann GmbH", allowSpaces: true);
            string conceptId   = Prompt(ed, "Concept ID",     "rossmann_standard",  allowSpaces: false);

            // ── 3. Save current DWG to temp ───────────────────────────────────
            string tempPath = Path.Combine(
                Path.GetTempPath(), $"lightingai_{Guid.NewGuid():N}.dwg");
            ed.WriteMessage("[LightingAI] Saving DWG snapshot…\n");
            db.SaveAs(tempPath, DwgVersion.Current);

            // ── 4 & 5. Upload + poll ──────────────────────────────────────────
            JobStatusResponse jobStatus;
            try
            {
                ed.WriteMessage("[LightingAI] Uploading to pipeline (30–120 s)…\n");
                var submit = ApiClient.SubmitPlan(tempPath, projectName, customer, conceptId);
                ed.WriteMessage($"[LightingAI] Job {submit.JobId} queued — polling…\n");

                jobStatus = ApiClient.PollJob(
                    submit.JobId,
                    msg => ed.WriteMessage($"[LightingAI]   {msg}\n"));
            }
            catch (Exception ex)
            {
                ed.WriteMessage($"[LightingAI] ✗  API error: {ex.Message}\n");
                return;
            }
            finally
            {
                try { File.Delete(tempPath); } catch { }
            }

            if (jobStatus.Status == "error")
            {
                ed.WriteMessage($"[LightingAI] ✗  Pipeline failed: {jobStatus.Message}\n");
                return;
            }

            var result = jobStatus.Result!;
            ed.WriteMessage(
                $"[LightingAI] ✓  Pipeline complete: " +
                $"{result.TotalLuminaires} luminaires  " +
                $"A:{result.TypeA} B:{result.TypeB} C:{result.TypeC} " +
                $"D:{result.TypeD} E:{result.TypeE}  " +
                $"{result.TotalWattage:F0} W\n");

            // ── 6–8. Write into the live DWG ──────────────────────────────────
            ed.WriteMessage("[LightingAI] Writing luminaires to drawing…\n");
            int placed = WriteLuminaires(doc, result, projectName, customer, conceptId);
            ed.WriteMessage($"[LightingAI] ✓  {placed} luminaires placed. Legend + title block added.\n");
        }

        // ── Core DWG writer ───────────────────────────────────────────────────
        private int WriteLuminaires(Document doc, PipelineResult result,
            string projectName, string customer, string conceptId)
        {
            var db = doc.Database;
            using var tr = db.TransactionManager.StartTransaction();

            LayerManager.EnsureLayers(tr, db);
            LayerManager.RemoveAiEntities(tr, db);  // idempotent re-run safety

            var ms         = (BlockTableRecord)tr.GetObject(db.CurrentSpaceId, OpenMode.ForWrite);
            var blockCache = new Dictionary<string, ObjectId>();

            foreach (var lp in result.Placed)
            {
                // Ensure block definition
                string key = $"{lp.LumiType}|{lp.ProductCode}";
                if (!blockCache.TryGetValue(key, out var blkId))
                {
                    blkId = BlockBuilder.EnsureBlock(tr, db, lp.LumiType, lp.ProductCode);
                    blockCache[key] = blkId;
                }

                short aci = TypeAci.TryGetValue(lp.LumiType, out var a) ? a : (short)6;

                // INSERT block reference
                var br = new BlockReference(new Point3d(lp.X, lp.Y, 0), blkId)
                {
                    Layer    = "AI-LUMINAIRES",
                    Rotation = lp.Rotation * Math.PI / 180.0,
                    Color    = Color.FromColorIndex(ColorMethod.ByAci, aci),
                };
                ms.AppendEntity(br);
                tr.AddNewlyCreatedDBObject(br, true);

                // Attach ATTRIB values to the INSERT
                var btrDef = (BlockTableRecord)tr.GetObject(blkId, OpenMode.ForRead);
                foreach (ObjectId attId in btrDef)
                {
                    if (tr.GetObject(attId, OpenMode.ForRead) is not AttributeDefinition attDef)
                        continue;
                    var attRef = new AttributeReference();
                    attRef.SetAttributeFromBlock(attDef, br.BlockTransform);
                    attRef.TextString = attDef.Tag switch
                    {
                        "TYPE"    => lp.LumiType,
                        "PRODUCT" => lp.ProductCode,
                        _         => attDef.TextString,
                    };
                    br.AttributeCollection.AppendAttribute(attRef);
                    tr.AddNewlyCreatedDBObject(attRef, true);
                }
            }

            if (result.Placed.Any())
            {
                DrawLegend(tr, ms, result);
                DrawTitleBlock(tr, ms, result, projectName, customer, conceptId);
            }

            tr.Commit();
            return result.Placed.Count;
        }

        // ── Legend ────────────────────────────────────────────────────────────
        private void DrawLegend(Transaction tr, BlockTableRecord ms, PipelineResult result)
        {
            double maxX = result.Placed.Max(p => p.X);
            double maxY = result.Placed.Max(p => p.Y);

            double lx = maxX + 12_000, ly = maxY;
            double rowH = 8_000, W = 110_000, pad = 2_000, th = 2_500;

            // Collect one representative per type
            var types    = new List<PlacedLuminaire>();
            var typeCnt  = result.Placed.GroupBy(p => p.LumiType)
                                        .ToDictionary(g => g.Key, g => g.Count());
            foreach (var lp in result.Placed)
                if (!types.Any(t => t.LumiType == lp.LumiType)) types.Add(lp);

            double totalH = rowH * (types.Count + 2) + 4_000;

            Rect(tr, ms, lx, ly - totalH, W, totalH, "AI-LEGEND");
            HLine(tr, ms, lx, ly - rowH, lx + W, "AI-LEGEND");
            Txt(tr, ms, "LEUCHTENLEGENDE / LEGEND", lx + pad, ly - rowH * 0.4, th, "AI-LEGEND");
            HLine(tr, ms, lx, ly - rowH * 2, lx + W, "AI-LEGEND");
            Txt(tr, ms, "Deckenausschnitt  AD:140 mm  EBT:110 mm  DA:128 mm",
                lx + pad, ly - rowH * 1.5, th * 0.8, "AI-LEGEND", 8);

            for (int i = 0; i < types.Count; i++)
            {
                var lp  = types[i];
                int qty = typeCnt.TryGetValue(lp.LumiType, out var q) ? q : 0;
                short aci = TypeAci.TryGetValue(lp.LumiType, out var a) ? a : (short)6;
                double ry = ly - rowH * (i + 3);
                double cr = 64;

                // Mini circle symbol
                Circle(tr, ms, lx + cr + pad, ry + rowH / 2, cr,  aci, "AI-LEGEND");
                Circle(tr, ms, lx + cr + pad, ry + rowH / 2, cr * 0.35, aci, "AI-LEGEND");
                Txt(tr, ms, $"Typ {lp.LumiType}",   lx + cr * 2 + pad * 3, ry + rowH * 0.6, th * 0.9, "AI-LEGEND", aci);
                Txt(tr, ms, lp.ProductCode,           lx + 16_000, ry + rowH * 0.7, th * 0.75, "AI-LEGEND");
                Txt(tr, ms, $"{lp.Wattage}W  {(int)lp.BeamAngleDeg}°  {lp.Description}",
                    lx + 16_000, ry + rowH * 0.28, th * 0.7, "AI-LEGEND", 8);
                Txt(tr, ms, $"× {qty}", lx + W - 18_000, ry + rowH * 0.5, th, "AI-LEGEND", aci);
                HLine(tr, ms, lx, ry, lx + W, "AI-LEGEND");
            }
        }

        // ── Title block ───────────────────────────────────────────────────────
        private void DrawTitleBlock(Transaction tr, BlockTableRecord ms,
            PipelineResult result, string projectName, string customer, string conceptId)
        {
            double minX = result.Placed.Min(p => p.X);
            double minY = result.Placed.Min(p => p.Y);

            double tx = minX, ty = minY - 80_000;
            double W = 180_000, H = 60_000, pad = 2_500, lh = 2_000, th = 3_000;
            double col1 = tx + W * 0.38, col2 = tx + W * 0.65;

            Rect(tr, ms, tx, ty, W, H, "AI-TITLEBLOCK");
            VLine(tr, ms, col1, ty, ty + H, "AI-TITLEBLOCK");
            VLine(tr, ms, col2, ty, ty + H, "AI-TITLEBLOCK");
            HLine(tr, ms, tx, ty + H * 0.60, tx + W, "AI-TITLEBLOCK");
            HLine(tr, ms, tx, ty + H * 0.35, tx + W, "AI-TITLEBLOCK");
            HLine(tr, ms, tx, ty + H * 0.15, tx + W, "AI-TITLEBLOCK");

            // Row 1: company
            Txt(tr, ms, "MAX FRANKE.led",
                tx + pad, ty + H * 0.69, th * 1.4, "AI-TITLEBLOCK", 6);
            Txt(tr, ms, "Osdorfer Landstrasse 174-176  ·  D-22549 Hamburg",
                tx + pad, ty + H * 0.64, lh, "AI-TITLEBLOCK");

            // Row 2: project details
            LblVal(tr, ms, "Projekt:",   projectName,
                   tx + pad, ty + H * 0.53, ty + H * 0.40, lh, th, "AI-TITLEBLOCK");
            LblVal(tr, ms, "Bauherr:",   customer,
                   col1 + pad, ty + H * 0.53, ty + H * 0.40, lh, th, "AI-TITLEBLOCK");
            LblVal(tr, ms, "Planinhalt:", $"Deckenrasterplan — {conceptId}",
                   col2 + pad, ty + H * 0.53, ty + H * 0.40, lh, th, "AI-TITLEBLOCK");

            // Row 3: scale / date / summary
            string now = DateTime.Now.ToString("dd.MM.yyyy");
            LblVal(tr, ms, "Maßstab:",  "1:75",
                   tx + pad,   ty + H * 0.27, ty + H * 0.17, lh, th, "AI-TITLEBLOCK");
            LblVal(tr, ms, "Datum:",    now,
                   col1 + pad, ty + H * 0.27, ty + H * 0.17, lh, th, "AI-TITLEBLOCK");
            LblVal(tr, ms, "Leuchten gesamt:",
                   $"{result.TotalLuminaires} Stk  ·  {result.TotalWattage:F0} W",
                   col2 + pad, ty + H * 0.27, ty + H * 0.17, lh, th, "AI-TITLEBLOCK");

            // Row 4: warning
            Txt(tr, ms,
                "Achtung: Alle Maße am Bau zu prüfen!  ·  " +
                "Attention: All dimensions to be checked locally!",
                tx + pad, ty + pad, lh * 0.9, "AI-TITLEBLOCK", 8);
        }

        // ── Drawing primitives ────────────────────────────────────────────────
        private static void Rect(Transaction tr, BlockTableRecord ms,
            double x, double y, double w, double h, string layer)
        {
            var pl = new Polyline();
            pl.AddVertexAt(0, new Point2d(x,     y),     0, 0, 0);
            pl.AddVertexAt(1, new Point2d(x + w, y),     0, 0, 0);
            pl.AddVertexAt(2, new Point2d(x + w, y + h), 0, 0, 0);
            pl.AddVertexAt(3, new Point2d(x,     y + h), 0, 0, 0);
            pl.Closed = true;
            pl.Layer  = layer;
            ms.AppendEntity(pl); tr.AddNewlyCreatedDBObject(pl, true);
        }

        private static void HLine(Transaction tr, BlockTableRecord ms,
            double x1, double y, double x2, string layer)
        {
            var ln = new Line(new Point3d(x1, y, 0), new Point3d(x2, y, 0))
                { Layer = layer, Color = Color.FromColorIndex(ColorMethod.ByAci, 8) };
            ms.AppendEntity(ln); tr.AddNewlyCreatedDBObject(ln, true);
        }

        private static void VLine(Transaction tr, BlockTableRecord ms,
            double x, double y1, double y2, string layer)
        {
            var ln = new Line(new Point3d(x, y1, 0), new Point3d(x, y2, 0))
                { Layer = layer, Color = Color.FromColorIndex(ColorMethod.ByAci, 8) };
            ms.AppendEntity(ln); tr.AddNewlyCreatedDBObject(ln, true);
        }

        private static void Txt(Transaction tr, BlockTableRecord ms,
            string text, double x, double y, double h, string layer, short aci = 7)
        {
            var t = new DBText
            {
                TextString = text,
                Position   = new Point3d(x, y, 0),
                Height     = h,
                Layer      = layer,
                Color      = Color.FromColorIndex(ColorMethod.ByAci, aci),
            };
            ms.AppendEntity(t); tr.AddNewlyCreatedDBObject(t, true);
        }

        private static void LblVal(Transaction tr, BlockTableRecord ms,
            string label, string value,
            double x, double labelY, double valueY,
            double labelH, double valueH, string layer)
        {
            Txt(tr, ms, label, x, labelY, labelH, layer, 8);
            Txt(tr, ms, value, x, valueY, valueH, layer, 7);
        }

        private static void Circle(Transaction tr, BlockTableRecord ms,
            double cx, double cy, double r, short aci, string layer)
        {
            var c = new Autodesk.AutoCAD.DatabaseServices.Circle(
                        new Point3d(cx, cy, 0), Vector3d.ZAxis, r)
                { Layer = layer, Color = Color.FromColorIndex(ColorMethod.ByAci, aci) };
            ms.AppendEntity(c); tr.AddNewlyCreatedDBObject(c, true);
        }

        // ── Prompt helper ─────────────────────────────────────────────────────
        private static string Prompt(Editor ed, string label, string def, bool allowSpaces)
        {
            var opt = new PromptStringOptions($"\n{label} [{def}]: ")
            {
                DefaultValue = def,
                AllowSpaces  = allowSpaces,
            };
            var result = ed.GetString(opt);
            return result.Status == PromptStatus.OK && !string.IsNullOrWhiteSpace(result.StringResult)
                ? result.StringResult
                : def;
        }
    }
}
