using System.Text;
using PdfiumViewer;
using UglyToad.PdfPig;
using SkiaSharp;

var app = WebApplication.CreateBuilder(args).Build();

app.UseStaticFiles();

app.MapGet("/", () => Results.Content(Html.IndexPage(), "text/html; charset=utf-8"));

app.MapPost("/compare", async (HttpRequest request) =>
{
    var form = await request.ReadFormAsync();
    var left = form.Files.GetFile("pdf1");
    var right = form.Files.GetFile("pdf2");
    if (left is null || right is null)
        return Results.Content(Html.IndexPage("Нужно выбрать два PDF файла"), "text/html; charset=utf-8");

    var precision = ParsePrecision(form["precision"].ToString());

    var tmp = Path.Combine(Path.GetTempPath(), "pdf-diff-highlighter-csharp", Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(tmp);

    var leftPath = Path.Combine(tmp, "left.pdf");
    var rightPath = Path.Combine(tmp, "right.pdf");
    await using (var s = File.Create(leftPath)) await left.CopyToAsync(s);
    await using (var s = File.Create(rightPath)) await right.CopyToAsync(s);

    var result = PdfComparator.Compare(leftPath, rightPath, precision);
    return Results.Content(Html.ResultPage(result), "text/html; charset=utf-8");
});

static int ParsePrecision(string? raw)
{
    if (!int.TryParse(raw, out var precision))
        return 75;
    return Math.Clamp(precision, 1, 100);
}

app.Run();

static class PdfComparator
{
    public static ComparisonResult Compare(string leftPath, string rightPath, int precision)
    {
        using var leftDoc = PdfDocument.Open(leftPath);
        using var rightDoc = PdfDocument.Open(rightPath);

        var maxPages = Math.Max(leftDoc.NumberOfPages, rightDoc.NumberOfPages);
        var pages = new List<PageResult>(maxPages);

        for (var i = 0; i < maxPages; i++)
        {
            var leftExists = i < leftDoc.NumberOfPages;
            var rightExists = i < rightDoc.NumberOfPages;

            if (!leftExists || !rightExists)
            {
                pages.Add(new PageResult
                {
                    PageNumber = i + 1,
                    TextChanged = true,
                    ImageChanged = true,
                    Note = "страница есть только в одном PDF",
                    TextRows = new List<TextRow>
                    {
                        new("Страница отличается", "(страница отсутствует)", "(страница отсутствует)")
                    }
                });
                continue;
            }

            var leftPageText = Normalize(leftDoc.GetPage(i + 1).Text);
            var rightPageText = Normalize(rightDoc.GetPage(i + 1).Text);
            var textRows = DiffLines(leftPageText, rightPageText);
            var diffImage = RenderDiffImage(leftPath, rightPath, i, precision);

            pages.Add(new PageResult
            {
                PageNumber = i + 1,
                TextChanged = textRows.Count > 0,
                ImageChanged = diffImage.HasDiff,
                TextRows = textRows,
                DiffImageDataUrl = diffImage.DataUrl,
                Note = diffImage.Note
            });
        }

        return new ComparisonResult
        {
            LeftPages = leftDoc.NumberOfPages,
            RightPages = rightDoc.NumberOfPages,
            ChangedPages = pages.Count(p => p.TextChanged || p.ImageChanged),
            Precision = precision,
            DiffThreshold = PrecisionToThreshold(precision),
            Pages = pages
        };
    }


    private static string Normalize(string text) => text.Replace("\r\n", "\n").Trim();

    private static List<TextRow> DiffLines(string left, string right)
    {
        if (left == right)
            return new List<TextRow>();

        var leftLines = left.Split('\n');
        var rightLines = right.Split('\n');
        var sm = new SequenceMatcher(leftLines, rightLines);
        var rows = new List<TextRow>();

        foreach (var op in sm.GetOpcodes())
        {
            if (op.Tag is "equal") continue;
            if (op.Tag is "delete" or "replace")
            {
                for (var i = op.AStart; i < op.AEnd; i++)
                    rows.Add(new TextRow("Удалено", leftLines[i], ""));
            }
            if (op.Tag is "insert" or "replace")
            {
                for (var i = op.BStart; i < op.BEnd; i++)
                    rows.Add(new TextRow("Добавлено", "", rightLines[i]));
            }
        }

        return rows;
    }

    private static DiffImage RenderDiffImage(string leftPath, string rightPath, int pageIndex, int precision)
    {
        using var leftDoc = PdfDocument.Load(leftPath);
        using var rightDoc = PdfDocument.Load(rightPath);
        using var leftBmp = leftDoc.Render(pageIndex, 144, 144, true);
        using var rightBmp = rightDoc.Render(pageIndex, 144, 144, true);

        using var leftImg = SKBitmap.Decode(BitmapToBytes(leftBmp)) ?? throw new InvalidOperationException("не удалось декодировать левую страницу");
        using var rightImg = SKBitmap.Decode(BitmapToBytes(rightBmp)) ?? throw new InvalidOperationException("не удалось декодировать правую страницу");

        var threshold = PrecisionToThreshold(precision);

        if (leftImg.Width != rightImg.Width || leftImg.Height != rightImg.Height)
        {
            using var canvasBmp = new SKBitmap(leftImg.Width + rightImg.Width + 24, Math.Max(leftImg.Height, rightImg.Height), SKColorType.Bgra8888, SKAlphaType.Premul);
            using var canvas = new SKCanvas(canvasBmp);
            canvas.Clear(SKColors.White);
            canvas.DrawBitmap(leftImg, 0, 0);
            canvas.DrawBitmap(rightImg, leftImg.Width + 24, 0);
            return new DiffImage(ToDataUrl(canvasBmp), true, $"разный размер страниц: {leftImg.Width}x{leftImg.Height} vs {rightImg.Width}x{rightImg.Height}");
        }

        using var diffBmp = new SKBitmap(leftImg.Width, leftImg.Height, SKColorType.Bgra8888, SKAlphaType.Premul);
        var changed = false;
        for (var y = 0; y < leftImg.Height; y++)
        {
            for (var x = 0; x < leftImg.Width; x++)
            {
                var l = leftImg.GetPixel(x, y);
                var r = rightImg.GetPixel(x, y);
                var d = Math.Abs(l.Red - r.Red) + Math.Abs(l.Green - r.Green) + Math.Abs(l.Blue - r.Blue);
                if (d > threshold)
                {
                    diffBmp.SetPixel(x, y, new SKColor(255, 64, 64));
                    changed = true;
                }
                else
                {
                    diffBmp.SetPixel(x, y, r);
                }
            }
        }

        return new DiffImage(ToDataUrl(diffBmp), changed, changed ? $"визуальные изменения обнаружены (порог {threshold})" : string.Empty);
    }

    private static int PrecisionToThreshold(int precision)
    {
        precision = Math.Clamp(precision, 1, 100);
        return Math.Max(1, (100 - precision) * 2);
    }

    private static byte[] BitmapToBytes(System.Drawing.Bitmap bmp)
    {
        using var ms = new MemoryStream();
        bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
        return ms.ToArray();
    }

    private static string ToDataUrl(SKBitmap bitmap)
    {
        using var image = SKImage.FromBitmap(bitmap);
        using var data = image.Encode(SKEncodedImageFormat.Png, 100);
        return "data:image/png;base64," + Convert.ToBase64String(data.ToArray());
    }
}

static class Html
{
    public static string IndexPage(string? error = null) => $"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF Compare C#</title>
  <link rel="stylesheet" href="/site.css">
</head>
<body>
  <main class="wrap">
    <h1>PDF Compare C#</h1>
    <p class="lead">Загрузите 2 многостраничных PDF. Поддерживаются drag and drop, кэш последних файлов и настройка точности сравнения.</p>
    {Error(error)}
    <form method="post" action="/compare" enctype="multipart/form-data" class="card compare-form" id="compare-form">
      <div class="drop-grid">
        <label class="dropzone" for="pdf1" data-slot="pdf1">
          <span class="dropzone-title">PDF 1</span>
          <span class="dropzone-hint">Перетащите файл сюда или нажмите для выбора</span>
          <input type="file" id="pdf1" name="pdf1" accept="application/pdf" required>
          <span class="dropzone-file" data-file-name="pdf1">Файл не выбран</span>
        </label>
        <div class="cache-panel" data-cache-slot="pdf1">
          <div class="cache-title">Кэш PDF 1</div>
          <div class="cache-list" data-cache-list="pdf1"></div>
        </div>
        <label class="dropzone" for="pdf2" data-slot="pdf2">
          <span class="dropzone-title">PDF 2</span>
          <span class="dropzone-hint">Перетащите файл сюда или нажмите для выбора</span>
          <input type="file" id="pdf2" name="pdf2" accept="application/pdf" required>
          <span class="dropzone-file" data-file-name="pdf2">Файл не выбран</span>
        </label>
        <div class="cache-panel" data-cache-slot="pdf2">
          <div class="cache-title">Кэш PDF 2</div>
          <div class="cache-list" data-cache-list="pdf2"></div>
        </div>
      </div>

      <div class="card precision-card">
        <div class="precision-head">
          <label for="precision">Точность сравнения</label>
          <output id="precision-value" for="precision">75</output>
        </div>
        <input type="range" id="precision" name="precision" min="1" max="100" value="75">
        <p class="muted">Выше значение — строже поиск мелких отличий.</p>
      </div>

      <button type="submit">Сравнить</button>
    </form>
  </main>
  <div id="viewer" class="viewer hidden" aria-hidden="true">
    <button id="viewer-close" class="viewer-close" type="button">×</button>
    <a id="viewer-download" class="viewer-download" download>Скачать</a>
    <img id="viewer-img" alt="preview">
  </div>
  <script src="/app.js" defer></script>
</body>
</html>
""";

    public static string ResultPage(ComparisonResult result)
    {
        var pages = new StringBuilder();
        foreach (var p in result.Pages)
        {
            pages.Append($"""
<section class="card page">
  <h2>Страница {p.PageNumber}</h2>
  <div class="flags">
    <span class="flag {(p.TextChanged ? "bad" : "good")}">Текст {(p.TextChanged ? "изменён" : "без изменений")}</span>
    <span class="flag {(p.ImageChanged ? "bad" : "good")}">Diff {(p.ImageChanged ? "есть" : "нет")}</span>
  </div>
  {(string.IsNullOrWhiteSpace(p.Note) ? "" : $"<p class='note'>{WebUtility.HtmlEncode(p.Note)}</p>")}
  <h3>Текст</h3>
  {(p.TextRows.Count == 0 ? "<p class='muted'>Текст без изменений.</p>" : TextTable(p.TextRows))}
  <h3>Изображение diff</h3>
  {(string.IsNullOrWhiteSpace(p.DiffImageDataUrl) ? "<p class='muted'>Diff-изображение недоступно.</p>" : $"<div class='diff-wrap'><a class='download' href='{p.DiffImageDataUrl}' download='page-{p.PageNumber}-diff.png'>Скачать PNG</a><button type='button' class='preview-btn' data-src='{p.DiffImageDataUrl}'>Полноэкранный просмотр</button><img class='diff-thumb' src='{p.DiffImageDataUrl}' alt='diff page {p.PageNumber}'></div>")}
</section>
""");
        }

        return $"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF Compare C#</title>
  <link rel="stylesheet" href="/site.css">
</head>
<body>
  <main class="wrap">
    <h1>PDF Compare C#</h1>
    <section class="card summary">
      <div>Страниц: {result.Pages.Count}</div>
      <div>Изменённых: {result.ChangedPages}</div>
      <div>Точность: {result.Precision}</div>
      <div>Порог diff: {result.DiffThreshold}</div>
    </section>
    {pages}
    <p><a href="/">Назад</a></p>
  </main>
  <div id="viewer" class="viewer hidden" aria-hidden="true">
    <button id="viewer-close" class="viewer-close" type="button">×</button>
    <a id="viewer-download" class="viewer-download" download>Скачать</a>
    <img id="viewer-img" alt="preview">
  </div>
  <script src="/app.js" defer></script>
</body>
</html>
""";

    private static string Error(string? error) => string.IsNullOrWhiteSpace(error) ? "" : $"<div class='error'>{WebUtility.HtmlEncode(error)}</div>";

    private static string TextTable(List<TextRow> rows)
    {
        var sb = new StringBuilder();
        sb.AppendLine("<table class='diff-table'><thead><tr><th>Тип</th><th>Исходный</th><th>Измененный</th></tr></thead><tbody>");
        foreach (var row in rows)
            sb.AppendLine($"<tr class='{(row.Kind == "Удалено" ? "del" : "ins")}'><td>{row.Kind}</td><td>{WebUtility.HtmlEncode(row.Source)}</td><td>{WebUtility.HtmlEncode(row.Changed)}</td></tr>");
        sb.AppendLine("</tbody></table>");
        return sb.ToString();
    }
}

record TextRow(string Kind, string Source, string Changed);

record PageResult
{
    public int PageNumber { get; init; }
    public bool TextChanged { get; init; }
    public bool ImageChanged { get; init; }
    public List<TextRow> TextRows { get; init; } = [];
    public string DiffImageDataUrl { get; init; } = string.Empty;
    public string Note { get; init; } = string.Empty;
}

record ComparisonResult
{
    public int LeftPages { get; init; }
    public int RightPages { get; init; }
    public int ChangedPages { get; init; }
    public int Precision { get; init; }
    public int DiffThreshold { get; init; }
    public List<PageResult> Pages { get; init; } = [];
}

record DiffImage(string DataUrl, bool HasDiff, string Note);

static class WebUtility
{
    public static string HtmlEncode(string s) => System.Net.WebUtility.HtmlEncode(s);
}

sealed class SequenceMatcher
{
    private readonly string[] _a;
    private readonly string[] _b;
    public SequenceMatcher(string[] a, string[] b) { _a = a; _b = b; }
    public IEnumerable<Opcode> GetOpcodes()
    {
        var m = _a.Length;
        var n = _b.Length;
        var dp = new int[m + 1, n + 1];
        for (int i = m - 1; i >= 0; i--)
            for (int j = n - 1; j >= 0; j--)
                dp[i, j] = _a[i] == _b[j] ? dp[i + 1, j + 1] + 1 : Math.Max(dp[i + 1, j], dp[i, j + 1]);

        var i0 = 0;
        var j0 = 0;
        while (i0 < m || j0 < n)
        {
            if (i0 < m && j0 < n && _a[i0] == _b[j0])
            {
                var ai = i0;
                var bj = j0;
                while (i0 < m && j0 < n && _a[i0] == _b[j0]) { i0++; j0++; }
                yield return new Opcode("equal", ai, i0, bj, j0);
            }
            else if (j0 < n && (i0 == m || dp[i0, j0 + 1] >= dp[i0 + 1, j0]))
            {
                var bj = j0;
                while (j0 < n && (i0 == m || dp[i0, j0 + 1] >= dp[i0 + 1, j0]) && (i0 >= m || _a[i0] != _b[j0])) j0++;
                yield return new Opcode("insert", i0, i0, bj, j0);
            }
            else if (i0 < m)
            {
                var ai = i0;
                while (i0 < m && (j0 == n || dp[i0 + 1, j0] > dp[i0, j0 + 1]) && (j0 >= n || _a[i0] != _b[j0])) i0++;
                yield return new Opcode("delete", ai, i0, j0, j0);
            }
        }
    }

    public readonly record struct Opcode(string Tag, int AStart, int AEnd, int BStart, int BEnd);
}
