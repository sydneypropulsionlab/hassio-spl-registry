// SPL induction certificate. Data comes in as JSON through sys.inputs.data (see certificate.py).
#let d = json(bytes(sys.inputs.at("data", default: "{}")))
#let blue = rgb("#0064de")
#let blue-dark = rgb("#004fb0")
#let tint = rgb("#e6f0fd")
#let ink = rgb("#1a1a1a")
#let grey = rgb("#383838")
#let muted = rgb("#6b7280")
#let line-col = rgb("#dde3ec")

#set page(paper: "a4", flipped: true, margin: 0mm)
#set text(font: "Inter", size: 10pt, fill: ink)
#set par(leading: 0.55em)

// Frame: blue band on the left, thin border inset.
#place(top + left, rect(width: 9mm, height: 100%, fill: blue))
#place(top + left, dx: 9mm, rect(width: 2.2mm, height: 100%, fill: ink))
#place(top + left, dx: 18mm, dy: 9mm, rect(width: 270mm, height: 192mm, stroke: 0.6pt + line-col, radius: 3mm))

#let label(t) = text(size: 7.5pt, weight: "semibold", fill: muted, tracking: 0.08em, upper(t))
#let fact(l, v, highlight: false) = box(
  width: 100%, inset: (x: 5mm, y: 4.2mm), radius: 2.5mm,
  fill: if highlight { tint } else { white },
  stroke: if highlight { 1.2pt + blue } else { 0.6pt + line-col },
  stack(spacing: 2.4mm, label(l),
    text(size: 13pt, weight: "bold", fill: if highlight { blue-dark } else { ink }, v)),
)

#place(top + left, dx: 30mm, dy: 17mm, block(width: 246mm, stack(
  grid(columns: (1fr, auto), align: (left + horizon, right + horizon),
    image("spl-logo.png", height: 22mm),
    stack(spacing: 2mm,
      align(right, text(size: 8pt, fill: muted)[Record #d.at("record_id", default: "")]),
      align(right, text(size: 8pt, fill: muted)[Issued #d.at("issued", default: "")])),
  ),
  12mm,
  text(size: 11pt, weight: "bold", fill: blue, tracking: 0.25em)[CERTIFICATE OF INDUCTION],
  11mm,
  text(size: 11pt, fill: grey)[This certifies that],
  6mm,
  text(font: "Inter Display", size: 34pt, weight: "bold")[#d.at("name", default: "")],
  9mm,
  text(size: 11pt, fill: grey)[has completed the],
  5mm,
  text(size: 20pt, weight: "semibold")[#d.at("induction", default: "")],
  4mm,
  text(size: 11pt, fill: muted)[#d.at("location", default: "") · Sydney Propulsion Lab],
  13mm,
  grid(columns: (1fr, 1fr, 1fr, 1fr), column-gutter: 4mm,
    fact("Induction date", d.at("induction_date", default: "")),
    fact("Valid for", d.at("valid_for", default: "")),
    fact("Renewal due", d.at("renewal_due", default: ""), highlight: true),
    fact("Assessed by", d.at("assessor", default: "")),
  ),
)))

// Grants access: sits at the bottom, just above the footer text
#if d.at("grants_access", default: ()).len() > 0 {
  place(bottom + left, dx: 30mm, dy: -29mm, block(width: 246mm,
    box(width: 100%, inset: (x: 5mm, y: 4mm), radius: 2.5mm, fill: ink,
      grid(columns: (auto, 1fr), column-gutter: 6mm, align: horizon,
        text(size: 7.5pt, weight: "semibold", fill: rgb("#9ca3af"), tracking: 0.08em)[GRANTS ACCESS],
        d.at("grants_access").map(a => box(inset: (x: 3mm, y: 1.6mm), radius: 1.5mm, fill: rgb("#2b2b2b"),
          text(size: 12pt, weight: "bold", fill: white, a))).join(h(2.5mm))))))
}

// Footer
#place(bottom + left, dx: 30mm, dy: -13mm, block(width: 246mm)[
  #line(length: 100%, stroke: 0.5pt + line-col)
  #v(2mm)
  #grid(columns: (1fr, auto), column-gutter: 10mm,
    text(size: 7.5pt, fill: muted)[
      #if d.at("sources", default: "") != "" [Based on #d.at("sources") · ]
      This certificate is valid only while the induction remains current; check the SPL induction register for its status. It covers this induction only and does not authorise use of other equipment or spaces.
    ],
    align(right, text(size: 7.5pt, fill: muted)[Sydney Propulsion Lab \ School of AMME, University of Sydney]),
  )
])
