import io
from datetime import datetime
import matplotlib.pyplot as plt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image
)
from reportlab.lib.units import inch

# ===================== THEME (Use strings for compatibility) =====================
PRIMARY = "#0f172a"
TEXT = "#334155"
MUTED = "#64748b"
BG_CARD = "#f8fafc"
BORDER = "#e5e7eb"

GREEN = "#10b981"
BLUE = "#3b82f6"

# ===================== PDF CLASS =====================
class ProfessionalDashboardPDF:
    def __init__(self, data: dict, org_id: str):
        self.data = data
        self.org_id = org_id
        self.styles = getSampleStyleSheet()
        self._setup_styles()

    # ---------- STYLES (SAFE) ----------
    def _setup_styles(self):
        def add(style):
            if style.name not in self.styles:
                self.styles.add(style)

        # Convert theme strings to ReportLab color objects
        c_primary = colors.HexColor(PRIMARY)
        c_muted = colors.HexColor(MUTED)

        add(ParagraphStyle(
            name="DashTitle",
            fontSize=26,
            fontName="Helvetica-Bold",
            textColor=c_primary,
            spaceAfter=10
        ))

        add(ParagraphStyle(
            name="DashSection",
            fontSize=15,
            fontName="Helvetica-Bold",
            textColor=c_primary,
            spaceBefore=26,
            spaceAfter=14
        ))

        add(ParagraphStyle(
            name="DashMuted",
            fontSize=9,
            textColor=c_muted
        ))

        add(ParagraphStyle(
            name="DashMetricValue",
            fontSize=22,
            fontName="Helvetica-Bold",
            textColor=c_primary,
            alignment=1
        ))

        add(ParagraphStyle(
            name="DashMetricLabel",
            fontSize=10,
            textColor=c_muted,
            alignment=1
        ))

    # ===================== CHARTS =====================
    def _donut_chart(self, ai: int, human: int):
        fig, ax = plt.subplots(figsize=(3.5, 3.5))
        
        if ai + human == 0:
            ax.text(0.5, 0.5, "No Data Available", ha="center", va="center", fontsize=10, color=MUTED)
            ax.set_axis_off()
        else:
            total = ai + human
            ax.pie(
                [ai, human],
                colors=[GREEN, BLUE],
                startangle=90,
                wedgeprops=dict(width=0.35),
                autopct=lambda p: f"{int(round(p * total / 100))}" if p == p else "0",
                pctdistance=0.75
            )

            ax.text(
                0, 0,
                "AI vs\nHuman",
                ha="center",
                va="center",
                fontsize=11,
                fontweight="bold",
                color="#1e293b"
            )
            ax.set(aspect="equal")

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, dpi=150, bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return buf

    def _funnel_chart(self, stages: list):
        labels = [s.get("label", "") for s in stages]
        values = [s.get("value", 0) for s in stages]

        fig, ax = plt.subplots(figsize=(5.5, 3.5))
        
        if not stages or sum(values) == 0:
            ax.text(0.5, 0.5, "No Data Available", ha="center", va="center", fontsize=10, color=MUTED)
            ax.set_axis_off()
        else:
            bars = ax.barh(labels, values, color=BLUE)
            ax.invert_yaxis()
            ax.set_title("Lead Funnel", fontsize=12, fontweight="bold")
            ax.spines[:].set_visible(False)
            ax.tick_params(left=False, bottom=False)
            ax.grid(axis="x", linestyle="--", alpha=0.25)

            max_val = max(values) if values else 1
            for bar in bars:
                width = bar.get_width()
                ax.text(
                    width + max_val * 0.02,
                    bar.get_y() + bar.get_height() / 2,
                    f"{int(width)}",
                    va="center",
                    fontsize=10
                )

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, dpi=150)
        plt.close()
        buf.seek(0)
        return buf

    # ===================== METRIC CARDS =====================
    def _metric_cards(self):
        stats = self.data.get("stats", {})

        cards = [
            ("Conversion Rate", f"{stats.get('conversionRate', {}).get('value', 0)}%"),
            ("Total Conversations", stats.get("totalConversations", {}).get("value", 0)),
            ("Hot Leads", stats.get("hotLeads", {}).get("value", 0)),
        ]

        card_cells = []
        for label, value in cards:
            card_cells.append(
                Table(
                    [
                        [Paragraph(label, self.styles["DashMetricLabel"])],
                        [Spacer(1, 6)],
                        [Paragraph(str(value), self.styles["DashMetricValue"])]
                    ],
                    colWidths=[2.2 * inch]
                )
            )

        table = Table([card_cells], colWidths=[2.3 * inch] * 3)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(BG_CARD)),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(BORDER)),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor(BORDER)),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 18),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ]))
        return table

    # ===================== BUILD PDF =====================
    def build(self):
        buffer = io.BytesIO()

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=48,
            rightMargin=48,
            topMargin=48,
            bottomMargin=48
        )

        story = []

        # ---------- HEADER ----------
        story.append(Paragraph(
            "HeidelAI – Analytics Report",
            self.styles["DashTitle"]
        ))
        story.append(Paragraph(
            f"Generated on {datetime.now().strftime('%d %b %Y, %H:%M')}",
            self.styles["DashMuted"]
        ))

        # ---------- METRICS ----------
        story.append(Spacer(1, 20))
        story.append(Paragraph(
            "Key Metrics",
            self.styles["DashSection"]
        ))
        story.append(self._metric_cards())

        # ---------- CHARTS ----------
        story.append(Spacer(1, 30))
        story.append(Paragraph(
            "Performance Overview",
            self.styles["DashSection"]
        ))

        ai = self.data.get("stats", {}).get("aiVsHuman", {}).get("ai", 0)
        human = self.data.get("stats", {}).get("aiVsHuman", {}).get("human", 0)

        charts = Table(
            [[
                Image(
                    self._funnel_chart(self.data.get("leadPatrol", [])),
                    5.5 * inch,
                    3.5 * inch
                ),
                Image(
                    self._donut_chart(ai, human),
                    3 * inch,
                    3 * inch
                )
            ]],
            colWidths=[5.7 * inch, 3.3 * inch]
        )

        charts.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(BG_CARD)),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(BORDER)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 14),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ]))

        story.append(charts)

        # ---------- FOOTER ----------
        def footer(canvas, _):
            canvas.setFont("Helvetica", 9)
            canvas.setFillColor(colors.HexColor(MUTED))
            canvas.drawCentredString(
                A4[0] / 2,
                25,
                f"Page {canvas.getPageNumber()} • Confidential – HeidelAI"
            )

        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        buffer.seek(0)
        return buffer


# ===================== PUBLIC API =====================
def generate_dashboard_pdf(stats_data: dict, org_id: str):
    return ProfessionalDashboardPDF(stats_data, org_id).build()
