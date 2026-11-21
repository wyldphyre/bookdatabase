// Chart helpers for the statistics page. Each draws into the canvas with the
// given id, and does nothing when that canvas isn't on the page (a section
// with no data isn't rendered).
(function () {
    const PALETTE = [
        '#667eea', '#764ba2', '#f5a623', '#50c878', '#ff6b6b',
        '#4ecdc4', '#45b7d1', '#96ceb4', '#ffeaa7', '#dfe6e9',
        '#a29bfe', '#fd79a8', '#00b894', '#e17055', '#0984e3'
    ];

    const money = value => '$' + Number(value).toFixed(2);
    const colourAt = i => PALETTE[i % PALETTE.length];

    // A bar chart. datasets: [{label, data, colour}], lined up with labels.
    // opts.money / opts.percent format the axis and tooltips; opts.stacked
    // stacks the datasets; opts.legend forces a legend even for one dataset
    // (when its name matters); opts.tooltip(index, dataset) overrides the tooltip.
    function bar(id, labels, datasets, opts = {}) {
        const canvas = document.getElementById(id);
        if (!canvas) return null;
        const several = datasets.length > 1 || !!opts.legend;
        const format = opts.money ? money : opts.percent ? value => value + '%' : value => value;
        return new Chart(canvas, {
            type: 'bar',
            data: {
                labels,
                datasets: datasets.map(d => ({
                    label: d.label,
                    data: d.data,
                    backgroundColor: d.colour,
                    borderRadius: opts.stacked ? 3 : 4,
                    maxBarThickness: 48
                }))
            },
            options: {
                responsive: true,
                // Taller on a phone, where a 2:1 chart is a sliver once the
                // legend's taken its share
                aspectRatio: window.innerWidth < 600 ? 1.2 : 2,
                plugins: {
                    legend: several ? { position: 'bottom' } : { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => opts.tooltip
                                ? opts.tooltip(ctx.dataIndex, ctx.datasetIndex)
                                : (several ? ctx.dataset.label + ': ' : '') + format(ctx.raw)
                        }
                    }
                },
                scales: {
                    x: { stacked: !!opts.stacked },
                    y: {
                        stacked: !!opts.stacked,
                        beginAtZero: true,
                        max: opts.percent ? 100 : undefined,
                        ticks: opts.money ? { callback: value => '$' + value }
                             : opts.percent ? { callback: value => value + '%' }
                             : { precision: 0 }
                    }
                }
            }
        });
    }

    // A doughnut (or opts.type 'pie') of {label: count}, with each slice's
    // share in its tooltip. opts.colours: one per slice, or a function of the
    // label; opts.onClick(index) makes the slices links.
    function pie(id, data, opts = {}) {
        const canvas = document.getElementById(id);
        if (!canvas) return null;
        const labels = Object.keys(data);
        const colours = typeof opts.colours === 'function' ? labels.map(opts.colours)
                      : opts.colours || labels.map((_, i) => colourAt(i));
        if (opts.onClick) canvas.style.cursor = 'pointer';
        return new Chart(canvas, {
            type: opts.type || 'doughnut',
            data: { labels, datasets: [{ data: Object.values(data), backgroundColor: colours }] },
            options: {
                responsive: true,
                plugins: {
                    legend: { position: 'bottom' },
                    tooltip: {
                        callbacks: {
                            label: ctx => {
                                const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                                return `${ctx.label}: ${ctx.raw} (${(100 * ctx.raw / total).toFixed(1)}%)`;
                            }
                        }
                    }
                },
                onClick: opts.onClick && ((event, elements) => {
                    if (elements.length) opts.onClick(elements[0].index);
                })
            }
        });
    }

    // Each book format keeps the same colour on every chart, by its place in
    // the full list of formats rather than in whichever ones a chart shows.
    function formatColour(allFormats) {
        return name => colourAt(Math.max(0, allFormats.indexOf(name)));
    }

    // {format: {year: amount}} as bar datasets lined up with years
    function perFormatDatasets(byFormat, years, colourOf) {
        return Object.keys(byFormat).map(fmt => ({
            label: fmt,
            data: years.map(year => byFormat[fmt][year] || 0),
            colour: colourOf(fmt)
        }));
    }

    window.StatsCharts = { bar, pie, formatColour, perFormatDatasets };
})();
