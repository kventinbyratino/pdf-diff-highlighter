using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Docnet.Core;
using Docnet.Core.Models;
using Docnet.Core.Readers;
using SkiaSharp;

var app = WebApplication.CreateBuilder(args).Build();

app.UseStaticFiles();

app.MapGet("/", (HttpRequest request, HttpResponse response) =>
{
    var visitor = UsageMetrics.ResolveVisitor(request, response);
    var metrics = UsageMetrics.RecordVisit(visitor);
    return Results.Content(Html.IndexPage(metrics), "text/html; charset=utf-8");
});

app.MapPost("/compare", async (HttpRequest request, HttpResponse response) =>
{
    var form = await request.ReadFormAsync();
    var left = form.Files.GetFile("pdf1");
    var right = form.Files.GetFile("pdf2");
    var precision = ParsePrecision(form["precision"].ToString());

    if (left is null || right is null)
    {
        var visitor = UsageMetrics.ResolveVisitor(request, response);
        var metrics = UsageMetrics.RecordVisit(visitor);
        return Results.Content(Html.IndexPage(metrics, "Нужно выбрать два PDF файла", precision), "text/html; charset=utf-8");
    }

    var tmp = Path.Combine(Path.GetTempPath(), "pdf-diff-highlighter-csharp", Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(tmp);

    try
    {
        var leftPath = Path.Combine(tmp, "left.pdf");
        var rightPath = Path.Combine(tmp, "right.pdf");
        await using (var s = File.Create(leftPath)) await left.CopyToAsync(s);
        await using (var s = File.Create(rightPath)) await right.CopyToAsync(s);

        var result = PdfComparator.Compare(leftPath, rightPath, precision);
        var visitor = UsageMetrics.ResolveVisitor(request, response);
        var metrics = UsageMetrics.RecordComparison(visitor);
        return Results.Content(Html.ResultPage(result, metrics), "text/html; charset=utf-8");
    }
    finally
    {
        try { Directory.Delete(tmp, recursive: true); } catch { /* temp cleanup is best-effort */ }
    }
});

static int ParsePrecision(string? raw)
{
    if (!int.TryParse(raw, out var precision))
        return 10;
    return Math.Clamp(precision, 1, 100);
}

app.Run();

static class UsageMetrics
{
    private static readonly object Gate = new();
    private static readonly string StorePath = Path.Combine(AppContext.BaseDirectory, "usage_metrics.json");
    private const string CookieName = "pdf_diff_visitor";

    public static string ResolveVisitor(HttpRequest request, HttpResponse response)
    {
        if (request.Cookies.TryGetValue(CookieName, out var existing) && !string.IsNullOrWhiteSpace(existing))
            return existing;

        var visitor = Guid.NewGuid().ToString("N");
        response.Cookies.Append(CookieName, visitor, new CookieOptions
        {
            MaxAge = TimeSpan.FromDays(730),
            SameSite = SameSiteMode.Lax,
            IsEssential = true
        });
        return visitor;
    }

    public static UsageMetricsSnapshot RecordVisit(string visitor)
    {
        lock (Gate)
        {
            var state = Read();
            state.Visitors.Add(visitor);
            Write(state);
            return state.ToSnapshot();
        }
    }

    public static UsageMetricsSnapshot RecordComparison(string visitor)
    {
        lock (Gate)
        {
            var state = Read();
            state.Visitors.Add(visitor);
            state.Comparisons += 1;
            Write(state);
            return state.ToSnapshot();
        }
    }

    private static UsageMetricsState Read()
    {
        try
        {
            if (!File.Exists(StorePath)) return new UsageMetricsState();
            var state = JsonSerializer.Deserialize<UsageMetricsState>(File.ReadAllText(StorePath));
            return state ?? new UsageMetricsState();
        }
        catch
        {
            return new UsageMetricsState();
        }
    }

    private static void Write(UsageMetricsState state)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(StorePath)!);
        File.WriteAllText(StorePath, JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true }));
    }
}

record UsageMetricsState
{
    public HashSet<string> Visitors { get; init; } = [];
    public int Comparisons { get; set; }
    public UsageMetricsSnapshot ToSnapshot() => new(Visitors.Count, Comparisons);
}

record UsageMetricsSnapshot(int UniqueUsers, int Comparisons);

static class PdfComparator
{
    public static ComparisonResult Compare(string leftPath, string rightPath, int precision)
    {
        using var leftDoc = OpenPdf(leftPath);
        using var rightDoc = OpenPdf(rightPath);

        var leftPageCount = leftDoc.GetPageCount();
        var rightPageCount = rightDoc.GetPageCount();
        var maxPages = Math.Max(leftPageCount, rightPageCount);
        var pages = new List<PageResult>(maxPages);

        for (var i = 0; i < maxPages; i++)
        {
            var leftExists = i < leftPageCount;
            var rightExists = i < rightPageCount;

            if (!leftExists || !rightExists)
            {
                pages.Add(new PageResult
                {
                    PageNumber = i + 1,
                    ImageChanged = true,
                    Note = "страница есть только в одном PDF"
                });
                continue;
            }

            var diffImage = RenderDiffImage(leftPath, rightPath, i, precision);

            pages.Add(new PageResult
            {
                PageNumber = i + 1,
                ImageChanged = diffImage.HasDiff,
                LeftImageDataUrl = diffImage.LeftDataUrl,
                DiffImageDataUrl = diffImage.DiffDataUrl,
                Note = diffImage.Note
            });
        }

        return new ComparisonResult
        {
            LeftPages = leftPageCount,
            RightPages = rightPageCount,
            ChangedPages = pages.Count(p => p.ImageChanged),
            Precision = precision,
            DiffThreshold = PrecisionToThreshold(precision),
            Pages = pages
        };
    }

    private static DiffImage RenderDiffImage(string leftPath, string rightPath, int pageIndex, int precision)
    {
        using var leftImg = RenderPage(leftPath, pageIndex);
        using var rightImg = RenderPage(rightPath, pageIndex);
        var leftDataUrl = ToDataUrl(leftImg);
        var threshold = PrecisionToThreshold(precision);

        if (leftImg.Width != rightImg.Width || leftImg.Height != rightImg.Height)
        {
            using var canvasBmp = new SKBitmap(leftImg.Width + rightImg.Width + 24, Math.Max(leftImg.Height, rightImg.Height), SKColorType.Bgra8888, SKAlphaType.Premul);
            using var canvas = new SKCanvas(canvasBmp);
            canvas.Clear(SKColors.White);
            canvas.DrawBitmap(leftImg, 0, 0);
            canvas.DrawBitmap(rightImg, leftImg.Width + 24, 0);
            return new DiffImage(leftDataUrl, ToDataUrl(canvasBmp), true, $"разный размер страниц: {leftImg.Width}x{leftImg.Height} vs {rightImg.Width}x{rightImg.Height}");
        }

        var width = leftImg.Width;
        var height = leftImg.Height;
        var mask = new bool[width, height];
        var changed = false;

        for (var y = 0; y < height; y++)
        {
            for (var x = 0; x < width; x++)
            {
                var l = leftImg.GetPixel(x, y);
                var r = rightImg.GetPixel(x, y);
                var d = Math.Abs(l.Red - r.Red) + Math.Abs(l.Green - r.Green) + Math.Abs(l.Blue - r.Blue);
                if (d > threshold)
                {
                    mask[x, y] = true;
                    changed = true;
                }
            }
        }

        using var diffBmp = new SKBitmap(width, height, SKColorType.Bgra8888, SKAlphaType.Premul);
        for (var y = 0; y < height; y++)
        {
            for (var x = 0; x < width; x++)
            {
                diffBmp.SetPixel(x, y, HasNeighbor(mask, width, height, x, y) ? SKColors.Red : rightImg.GetPixel(x, y));
            }
        }

        return new DiffImage(leftDataUrl, ToDataUrl(diffBmp), changed, changed ? $"визуальные изменения обнаружены (порог {threshold})" : string.Empty);
    }

    private static bool HasNeighbor(bool[,] mask, int width, int height, int x, int y)
    {
        for (var yy = Math.Max(0, y - 1); yy <= Math.Min(height - 1, y + 1); yy++)
            for (var xx = Math.Max(0, x - 1); xx <= Math.Min(width - 1, x + 1); xx++)
                if (mask[xx, yy]) return true;
        return false;
    }

    private static int PrecisionToThreshold(int precision)
    {
        precision = Math.Clamp(precision, 1, 100);
        return Math.Max(1, (int)Math.Round(12 - ((precision - 1) * 11 / 99.0)));
    }

    private static IDocReader OpenPdf(string path)
    {
        return DocLib.Instance.GetDocReader(File.ReadAllBytes(path), new PageDimensions(1800, 1800));
    }

    private static SKBitmap RenderPage(string path, int pageIndex)
    {
        using var doc = OpenPdf(path);
        using var page = doc.GetPageReader(pageIndex);
        var width = page.GetPageWidth();
        var height = page.GetPageHeight();
        var bytes = page.GetImage();
        var bitmap = new SKBitmap(width, height, SKColorType.Bgra8888, SKAlphaType.Premul);
        Marshal.Copy(bytes, 0, bitmap.GetPixels(), Math.Min(bytes.Length, bitmap.ByteCount));
        return bitmap;
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
    public static string IndexPage(UsageMetricsSnapshot metrics, string? error = null, int precision = 10) => $"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Сравнение PDF чертежей</title>
  <link rel="stylesheet" href="/site.css">
</head>
<body>
  <main class="wrap">
    <section class="hero card">
      <div class="eyebrow">PDF diff</div>
      <h1>Сравнение PDF чертежей</h1>
      <p class="lead">Загрузите два PDF, настройте точность и сравните исходный лист с маской изменений.</p>
    </section>

    {UsageBlock(metrics)}

    {Error(error)}

    <section class="workspace">
      <form method="post" action="/compare" enctype="multipart/form-data" class="card panel" id="compare-form">
        <div class="upload-grid">
          <section class="slot-card dropzone" data-target="pdf1">
            <button type="button" class="slot-clear" data-clear-file="pdf1" aria-label="Удалить чертеж 1" hidden>
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M4 7h16"></path>
                <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"></path>
                <path d="M8 7l1 12h6l1-12"></path>
                <path d="M10 11v5M14 11v5"></path>
              </svg>
            </button>
            <label class="upload-field" for="pdf1">
              <span class="sr-only">Выбрать PDF 1</span>
              <span class="dropzone-art" aria-hidden="true">
                <svg viewBox="0 0 64 64" focusable="false" aria-hidden="true">
                  <rect x="14" y="10" width="26" height="18" rx="3"></rect>
                  <rect x="24" y="20" width="26" height="18" rx="3"></rect>
                  <path d="M31 30v10M26 35h10"></path>
                </svg>
              </span>
              <span class="slot-status" data-upload-status="pdf1">Чертеж 1 — статус: не загружен</span>
              <input type="file" id="pdf1" name="pdf1" accept="application/pdf" required class="file-input">
            </label>
          </section>

          <section class="slot-card dropzone" data-target="pdf2">
            <button type="button" class="slot-clear" data-clear-file="pdf2" aria-label="Удалить чертеж 2" hidden>
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M4 7h16"></path>
                <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"></path>
                <path d="M8 7l1 12h6l1-12"></path>
                <path d="M10 11v5M14 11v5"></path>
              </svg>
            </button>
            <label class="upload-field" for="pdf2">
              <span class="sr-only">Выбрать PDF 2</span>
              <span class="dropzone-art" aria-hidden="true">
                <svg viewBox="0 0 64 64" focusable="false" aria-hidden="true">
                  <rect x="14" y="10" width="26" height="18" rx="3"></rect>
                  <rect x="24" y="20" width="26" height="18" rx="3"></rect>
                  <path d="M31 30v10M26 35h10"></path>
                </svg>
              </span>
              <span class="slot-status" data-upload-status="pdf2">Чертеж 2 — статус: не загружен</span>
              <input type="file" id="pdf2" name="pdf2" accept="application/pdf" required class="file-input">
            </label>
          </section>
        </div>

        <div class="precision-row">
          <label for="precision">Точность сравнения</label>
          <output class="precision-value" for="precision">{precision}</output>
          <input id="precision" class="precision-input" name="precision" type="range" min="1" max="100" value="{precision}">
        </div>

        <div class="form-actions">
          <button type="button" class="secondary" data-reset-all>Сбросить</button>
          <button type="submit">Сравнить</button>
        </div>
      </form>
    </section>
  </main>

  {Viewer()}
  <script src="/app.js" defer></script>
</body>
</html>
""";

    public static string ResultPage(ComparisonResult result, UsageMetricsSnapshot metrics)
    {
        return $"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Сравнение PDF чертежей</title>
  <link rel="stylesheet" href="/site.css">
</head>
<body>
  <main class="wrap">
    <section class="hero card">
      <div class="eyebrow">PDF diff</div>
      <h1>Сравнение PDF чертежей</h1>
      <p class="lead">Загрузите два PDF, настройте точность и сравните исходный лист с маской изменений.</p>
    </section>

    {UsageBlock(metrics)}

    <section class="workspace">
      <form method="post" action="/compare" enctype="multipart/form-data" class="card panel" id="compare-form">
        <div class="upload-grid">
          <section class="slot-card dropzone" data-target="pdf1">
            <button type="button" class="slot-clear" data-clear-file="pdf1" aria-label="Удалить чертеж 1" hidden></button>
            <label class="upload-field" for="pdf1"><span class="sr-only">Выбрать PDF 1</span><span class="slot-status" data-upload-status="pdf1">Чертеж 1 — статус: не загружен</span><input type="file" id="pdf1" name="pdf1" accept="application/pdf" required class="file-input"></label>
          </section>
          <section class="slot-card dropzone" data-target="pdf2">
            <button type="button" class="slot-clear" data-clear-file="pdf2" aria-label="Удалить чертеж 2" hidden></button>
            <label class="upload-field" for="pdf2"><span class="sr-only">Выбрать PDF 2</span><span class="slot-status" data-upload-status="pdf2">Чертеж 2 — статус: не загружен</span><input type="file" id="pdf2" name="pdf2" accept="application/pdf" required class="file-input"></label>
          </section>
        </div>
        <div class="precision-row">
          <label for="precision">Точность сравнения</label>
          <output class="precision-value" for="precision">{result.Precision}</output>
          <input id="precision" class="precision-input" name="precision" type="range" min="1" max="100" value="{result.Precision}">
        </div>
        <div class="form-actions"><button type="button" class="secondary" data-reset-all>Сбросить</button><button type="submit">Сравнить</button></div>
      </form>
    </section>

    <section class="card results-card">
      <header class="result-head"><div><div class="eyebrow">Результат</div><h2>Сравнение листов</h2></div></header>
      <div class="results-body {(result.Pages.Count > 1 ? "results-body-with-sidebar" : "")}">
        {PagesSidebar(result.Pages)}
        <div class="results-pages">
          {Pages(result.Pages)}
        </div>
      </div>
    </section>
  </main>

  {Viewer()}
  <script src="/app.js" defer></script>
</body>
</html>
""";
    }

    private static string PagesSidebar(List<PageResult> pages)
    {
        if (pages.Count <= 1) return "";
        var buttons = new StringBuilder();
        foreach (var p in pages)
        {
            buttons.Append($"""
<button type="button" class="page-nav-btn" data-page-target="page-{p.PageNumber}" aria-label="Перейти к странице {p.PageNumber}">
  <span class="page-nav-index">{p.PageNumber}</span>
  <span class="page-nav-label">Лист {p.PageNumber}</span>
</button>
""");
        }
        return $"""
<aside class="pages-sidebar" aria-label="Меню листов">
  <div class="pages-sidebar-head"><div class="eyebrow">Листы</div><h3>Меню листов</h3></div>
  <nav class="page-nav page-nav-vertical" aria-label="Страницы PDF">{buttons}</nav>
</aside>
""";
    }

    private static string Pages(List<PageResult> pages)
    {
        var sb = new StringBuilder();
        foreach (var p in pages)
        {
            var body = !string.IsNullOrWhiteSpace(p.LeftImageDataUrl) && !string.IsNullOrWhiteSpace(p.DiffImageDataUrl)
                ? $"""
<div class="page-compare" data-compare-slider>
  <div class="compare-caption" aria-hidden="true"><span>Исходный файл</span><span>Маска изменений</span></div>
  <div class="compare-stage" style="--split: 50%;">
    <img class="compare-layer compare-source" src="{p.LeftImageDataUrl}" alt="Исходный лист {p.PageNumber}">
    <div class="compare-overlay"><img class="compare-layer compare-diff" src="{p.DiffImageDataUrl}" alt="Маска изменений лист {p.PageNumber}"></div>
    <div class="compare-divider" aria-hidden="true"></div>
    <input class="compare-range" data-compare-range type="range" min="0" max="100" value="50" aria-label="Бегунок сравнения страницы {p.PageNumber}">
  </div>
  <div class="slot-actions">
    <a class="download" href="{p.DiffImageDataUrl}" download="page-{p.PageNumber}-diff.png">Скачать сравнение</a>
    <button type="button" class="preview-btn" data-viewer-src="{p.DiffImageDataUrl}" data-download="page-{p.PageNumber}-diff.png">Полноэкранный просмотр</button>
  </div>
</div>
"""
                : $"<div class=\"page-note\">{WebUtility.HtmlEncode(string.IsNullOrWhiteSpace(p.Note) ? "Сравнение недоступно для этой страницы" : p.Note)}</div>";
            sb.Append($"""
<article class="page card" id="page-{p.PageNumber}">
  <div class="page-head"><h3>Страница {p.PageNumber}</h3></div>
  {body}
</article>
""");
        }
        return sb.ToString();
    }

    private static string UsageBlock(UsageMetricsSnapshot metrics) => $"""
<section class="usage-metrics card" aria-label="Статистика сервиса">
  <div class="usage-metric">
    <span class="usage-metric-label">Сравнили чертежей</span>
    <strong class="usage-metric-value" data-usage-comparisons>{metrics.Comparisons}</strong>
  </div>
</section>
""";

    private static string Viewer() => """
<div id="viewer" class="viewer hidden" aria-hidden="true">
  <button id="viewer-close" class="viewer-close" type="button" aria-label="Закрыть">×</button>
  <a id="viewer-download" class="viewer-download" download>Скачать</a>
  <img id="viewer-img" alt="preview">
</div>
""";

    private static string Error(string? error) => string.IsNullOrWhiteSpace(error) ? "" : $"<div class='error'>{WebUtility.HtmlEncode(error)}</div>";
}

record PageResult
{
    public int PageNumber { get; init; }
    public bool ImageChanged { get; init; }
    public string LeftImageDataUrl { get; init; } = string.Empty;
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

record DiffImage(string LeftDataUrl, string DiffDataUrl, bool HasDiff, string Note);

static class WebUtility
{
    public static string HtmlEncode(string s) => System.Net.WebUtility.HtmlEncode(s);
}
