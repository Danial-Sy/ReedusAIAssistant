// Reedus differential-drive chassis, parametric, sized for the Bambu X2D bed.
// motor + caster dims come straight from the vendor drawings (see the dims blocks below).
//
// render a part:  openscad -o tub.stl -D 'part="tub"' reedus_chassis.scad
// parts: tub, deck, mobodeck, mobowall_l/r, mobopuck(s), moborail(s), moborail_L,
//        mobowasher(s), hub, tire, spacer, assembly, assembly_tub/deck/mobodeck
//
// notes on the design:
//   * motors sit in epoxy-putty cradles (loose stepped bore, zip-tie stations), face flush
//     on the inner wall, shaft out a Ø26 opening
//   * caster plate bolts flat to the floor, ball drops through a hole; 7mm spacer levels it
//   * flat open tub floor, parts tape/velcro down. footprint 248 x 228
//   * mobodeck is a honeycomb plate with floating standoff pucks / T-slot rails because the
//     X99 hole pattern isn't published anywhere and the guessed positions never fit
//   * deck, hub, tire are locked - printed, don't reprint

part = "assembly"; // override with: openscad -D "part=\"mobowall_r\"" ...
// an unknown part name must NOT fall through to the full assembly (windows -D quirk)
$fn = 64;

// --- motor dims (JGB37-520-EN, 12 V 76 RPM), from the vendor drawing ---
motor_d    = 37;     // gearbox OD (Ø37 +0/-0.36)
can_d      = 32;     // motor can OD (Ø32 -0.2)
gb_len     = 26.5;   // gearbox length (ratio-131 class per L table)
can_len    = 22;     // motor can length (22 -0.2)
enc_len    = 4.5;    // encoder board + cap stack (3 + 1 + slack)
enc_d      = 32.6;   // encoder disc Ø32.3 + slack
boss_d     = 12;     // pilot boss Ø12 -0.2
boss_len   = 6;      // boss height (6 +0.2)
shaft_d    = 6.0;    // Ø6 -0.2 D-shaft
shaft_flat = 5.5;    // across-flat 5.5 -0.1
shaft_len  = 15;     // shaft beyond boss (15 ±0.3); total protrusion 21 ±0.2
motor_off  = 7;      // shaft offset ABOVE gearbox center (user-confirmed)

// --- caster dims (ball transfer unit) ---
caster_h     = 20;   // mounting face -> ball crown (2 cm, photo)
cast_ball_d  = 15;   // Ø1.5 cm ball
cast_plate_l = 50;   // 5 cm plate length (holes along this axis)
cast_plate_w = 35;   // 3.5 cm plate width
cast_plate_t = 2.2;
cast_hole_cc = 40;   // user-measured ~40 mm c-c
cast_dome_hole = 30; // through-floor clearance for dome + ball (loose fit;
                     // keeps 3.3 mm web to the Ø3.4 bolt holes @ ±20)

// --- verified component dims ---
bat_l      = 181;    // 12V 18Ah SLA, longest face (UB12180 class)
bat_w      = 167;
bat_h      = 77;
bat_tray_clr = 4.0;

fuse_l     = 132;    // fuse block footprint (user re-confirmed)
fuse_w     = 82;
fuse_h     = 36.5;

wheel_d    = 80;     // printed wheel outer diameter (user-confirmed)
wheel_w    = 28;
face_bcd   = 31;     // 6× M3 (3.5 deep) face-mount bolt circle, unused, FYI
hex_clr_d  = 14;
shaft_use  = 13;

// --- motherboard (MOUGOL X99) ---
mobo_l     = 215;    // measured PCB 215 × 190 mm
mobo_w     = 190;
mobo_h     = 1.6;
mobo_io_h  = 32;
cooler_h   = 35;
ram_h      = 31;
picopsu_l  = 53;
picopsu_w  = 20;
picopsu_h  = 37;

// --- tub electronics ---
bts_l      = 50;
bts_w      = 50;
bts_h      = 15;
lvc_l      = 57;
lvc_w      = 42;
lvc_h      = 19;
buck_l     = 45;
buck_w     = 21;
buck_h     = 14;
ecookie_l  = 172.7;  // full-size ElectroCookie (user-confirmed)
ecookie_w  = 52.1;
ecookie_h  = 1.6;
pico_l     = 51;
pico_w     = 21;

// --- structure ---
wall       = 4;
floor_t    = 4;
clr_gnd    = 9;
tol        = 0.4;
plate_xy   = 256;
bed_margin = 3;

m3_pilot   = 2.8;
m3_clear   = 3.4;
m3_nut_af  = 7.0;

axle_z     = wheel_d/2;          // 40
floor_z    = clr_gnd;            // 9
floor_top  = clr_gnd + floor_t;  // 13
inner_w    = 220;                // LOCKED, printed tub/deck bosses (was mobo_l+8)
outer_w    = inner_w + 2*wall;   // 228
half_w     = outer_w/2;          // 114
body_z     = axle_z - motor_off; // 33, motor cylinder axis height

// battery center slightly aft of the drive axle for stability
bat_off_y  = -18;
deck_z     = 62;
deck_t     = 4;
tray_wall  = 3;
tray_h     = 22;

tier_span  = 192;                // LOCKED, printed battery deck (was max(bat_w,182)+2*tray_wall+4)
tail_ext   = 14;                        // legacy (deck geometry only)
nose_ext   = 10;
deck_y0    = bat_off_y - tier_span/2 - 2;   // -116  (deck unchanged)
deck_y1    = bat_off_y + tier_span/2 + nose_ext;  // 88
deck_x     = outer_w - 1;

mobo_deck_z = deck_z + deck_t + bat_h + 8;
mobo_deck_t = 4;
leg_d      = 14;     // legacy (old column OD; walls replace columns)
skirt_h    = 18;

// --- mobodeck: honeycomb plate + floating standoff pucks ---
// no standoff XY lives in CAD. the plate is a hex grid and six pucks (each holding a brass
// M3 standoff) get epoxied wherever the board's real holes land - the board is the jig.
// puck across-flats 16 > cell circumdiameter 13.86, so a puck always overlaps rib material.
standoff_pilot  = 2.6;      // still used by mobowall() top screws
_inch           = 25.4;

// plate runs further aft than the deck so a puck near the I/O edge (y=-113) is supported
mobo_y0    = deck_y0 - 6;        // -122  (was -116; only 3 mm of margin)
mobo_y1    = deck_y1;            //   88

// honeycomb
hex_af     = 12.0;                          // cell across-flats
hex_wall   = 3.5;                           // rib thickness
hex_r      = hex_af / sqrt(3);              // 6.928 circumradius
hex_r2     = hex_r + hex_wall / sqrt(3);    // 8.949 pitch radius
hex_dx     = 1.5 * hex_r2;                  // 13.42 column pitch
hex_dy     = sqrt(3) * hex_r2;              // 15.50 row pitch
hex_band   = 11;                            // solid perimeter band (covers the
                                            // ±108 wall screws at x=±108)

// standoff puck (separate part)
puck_af    = 16.0;   // hex across-flats, must exceed hex circumdia 13.86
puck_h     = 7.0;    // + brass standoff length = board clearance
puck_cav_d = 12.0;   // putty cavity dia (open at bottom, rivets through cells)
puck_cav_h = 4.0;
puck_boss_d = 8.0;   // central boss inside the cavity, carries the pilot
puck_pilot = 2.85;   // M3 male stud threads straight into PETG
brass_h    = 6.0;    // brass M3 male-female standoff body length
mobo_clear = puck_h + brass_h;   // 13 mm PCB underside clearance

echo("hex grid: af", hex_af, "wall", hex_wall, "dx", hex_dx, "dy", hex_dy);
echo("puck af", puck_af, "vs cell circumdia", 2*hex_r);

// side walls replace the old 4 columns, mount on the existing deck leg_y holes
wall_t     = 8;                   // wall thickness
wall_x     = inner_w/2 - 2;       // same X as old column centers (±108)
wall_h     = mobo_deck_z - deck_z - deck_t;  // span battery height
wall_foot_ly = 11;                // matches deck hex pockets at leg_y ±11

// tub footprint: longer tail for electronics, blunter nose
nose_y     = deck_y1 + wall + 4;   // 96
tail_y     = -152;
caster_y   = -70;                  // caster mid-rear, clear of everything

footprint_x = outer_w;
footprint_y = nose_y - tail_y;     // 248
echo("BED FIT check, footprint (mm):", footprint_x, "×", footprint_y,
     "(limit", plate_xy - 2*bed_margin, ")");

// deck bosses, 4 corners only (deck itself keeps 6 holes)
tub_boss_y = [bat_off_y-75, bat_off_y+75];          // [-93, 57]
boss_y     = [bat_off_y-75, bat_off_y, bat_off_y+75]; // deck keeps its 6 holes
leg_y      = [bat_off_y-75, bat_off_y+75];

// --- motor cradle (epoxy putty) ---
putty_gap  = 3.0;                      // per-side putty allowance (motor taped;
                                       // deliberately loose, never a tight drop-in)
cradle_hw  = motor_d/2 + putty_gap + 3;   // 23.5, half-width incl. 3 mm wall
cradle_x0  = 62;                       // block start (encoder hangs free aft)
zip_w      = 7;                        // fits zip ties AND narrow velcro
zip_x      = [70, 96];                 // strap stations: motor-can + gearbox
// motor face registers on the inner wall at x=110; gearbox 83.5..110, can 61.5..83.5, encoder 57..61.5

// --- tub floor layout (advisory, everything tapes down) ---
// >= 3mm between items, >= 11mm air above the tallest (fuse, 49.5) under the deck (62)
fuse_cx = 0;    fuse_cy = 22;    // ROTATED: 82 wide (X) × 132 deep (Y)
bts_l_cx = -75; bts_l_cy = 56;   // nose-left
bts_r_cx =  75; bts_r_cy = 56;   // nose-right
lvc_cx  = -65;  lvc_cy  = -68;   // left-mid; buck stacks on top (standoffs/tape)
buck_cx = -65;  buck_cy = -68;
buck_z_off = lvc_h + 5;
ec_cx   = 0;    ec_cy   = -119;  // full-width rear bay
// spare wiring bay: x 30..110, y -89..-47 (switch, XT60 tails, slack)

spacer_h = caster_h - floor_top;   // 7, levels caster w/ 80 mm wheels

// --- helpers ---
module rrect(x, y, r=2) offset(r=r) offset(delta=-r) square([x, y], center=true);

module slot(l, w) hull() {
    translate([-l/2+w/2,0]) circle(d=w);
    translate([ l/2-w/2,0]) circle(d=w);
}

module footprint()
    offset(r=6) offset(delta=-6)
    polygon([[-half_w, tail_y], [half_w, tail_y],
             [half_w, deck_y1+wall], [half_w*0.55, nose_y],
             [-half_w*0.55, nose_y], [-half_w, deck_y1+wall]]);

module vent_row(n, l=22, w=3.5, pitch=7)
    for (i=[0:n-1]) translate([0, i*pitch]) slot(l, w);

module ghost_box(l, w, h, r=1) {
    translate([-l/2, -w/2, 0]) minkowski() {
        cube([l-2*r, w-2*r, h-r]);
        translate([r,r,0]) cylinder(r=r, h=r);
    }
}

// --- component ghosts (collision / assembly check) ---
module ghost_fuse()   color("Gold", 0.75)     translate([fuse_cx, fuse_cy, floor_top])
                          ghost_box(fuse_w, fuse_l, fuse_h);   // rotated 90°
module ghost_bts_l()  color("ForestGreen", 0.8) translate([bts_l_cx, bts_l_cy, floor_top])
                          ghost_box(bts_l, bts_w, bts_h);
module ghost_bts_r()  color("ForestGreen", 0.8) translate([bts_r_cx, bts_r_cy, floor_top])
                          ghost_box(bts_l, bts_w, bts_h);
module ghost_ecookie() color("DarkOrange", 0.85) translate([ec_cx, ec_cy, floor_top])
                          ghost_box(ecookie_l, ecookie_w, ecookie_h + 8);
module ghost_pico()   color("LimeGreen", 0.9) translate([ec_cx - 30, ec_cy, floor_top + ecookie_h + 8])
                          ghost_box(pico_l, pico_w, 3);
module ghost_lvc()    color("Cyan", 0.8)      translate([lvc_cx, lvc_cy, floor_top])
                          ghost_box(lvc_l, lvc_w, lvc_h);
module ghost_buck()   color("Violet", 0.8)    translate([buck_cx, buck_cy, floor_top + buck_z_off])
                          ghost_box(buck_l, buck_w, buck_h);
module ghost_battery() color("Yellow", 0.55)  translate([0, bat_off_y, deck_z+deck_t])
                          ghost_box(bat_l, bat_w, bat_h);
module ghost_mobo() {
    color("RoyalBlue", 0.65) translate([0, bat_off_y, mobo_deck_z + mobo_deck_t + mobo_clear])
        ghost_box(mobo_l, mobo_w, mobo_h);
    color("Silver", 0.7) translate([0, bat_off_y - mobo_w/2 + 18, mobo_deck_z + mobo_deck_t + mobo_clear + mobo_h])
        ghost_box(mobo_l - 20, mobo_io_h, mobo_io_h);
    color("Red", 0.6) for (sx=[-1,1])
        translate([sx*38, bat_off_y + 25, mobo_deck_z + mobo_deck_t + mobo_clear + mobo_h])
            ghost_box(8, 30, ram_h);
    color("Gray", 0.55) translate([0, bat_off_y + 15, mobo_deck_z + mobo_deck_t + mobo_clear + mobo_h])
        cylinder(d=90, h=cooler_h);
    color("Gold", 0.85) translate([mobo_l/2 - 30, bat_off_y + mobo_w/2 - 25,
                                   mobo_deck_z + mobo_deck_t + mobo_clear + mobo_h])
        ghost_box(picopsu_l, picopsu_w, picopsu_h);
}

// caster shown with the 7mm spacer so the ball lands at ground 0
module ghost_caster() translate([0, caster_y, 0]) {
    color("LightSteelBlue", 0.8) translate([0, 0, floor_top])
        difference() {
            ghost_box(cast_plate_l + 6, cast_plate_w + 4, spacer_h);
            translate([0,0,-1]) cylinder(d=cast_dome_hole, h=spacer_h+2);
        }
    color("Black", 0.7) union() {
        translate([0, 0, floor_top + spacer_h])
            ghost_box(cast_plate_l, cast_plate_w, cast_plate_t);
        translate([0, 0, cast_ball_d/2]) sphere(d=cast_ball_d);    // ball @ ground
        cylinder(d1=22, d2=25, h=floor_top + spacer_h);            // dome body
    }
}

module ghost_motor_right() color("DimGray", 0.85) {
    translate([inner_w/2 - gb_len, 0, body_z]) rotate([0,90,0])
        cylinder(d=motor_d, h=gb_len);                         // gearbox
    translate([inner_w/2 - gb_len - can_len, 0, body_z]) rotate([0,90,0])
        cylinder(d=can_d, h=can_len);                          // motor can
    translate([inner_w/2 - gb_len - can_len - enc_len, 0, body_z]) rotate([0,90,0])
        cylinder(d=enc_d, h=enc_len);                          // encoder
    translate([inner_w/2, 0, axle_z]) rotate([0,90,0]) {
        cylinder(d=boss_d, h=boss_len);                        // pilot boss
        translate([0,0,boss_len]) cylinder(d=shaft_d, h=shaft_len); // D-shaft
    }
}
module ghost_motors() for (sx=[0,1]) mirror([sx,0,0]) ghost_motor_right();

module tub_electronics() {
    ghost_fuse(); ghost_bts_l(); ghost_bts_r();
    ghost_ecookie(); ghost_pico(); ghost_lvc(); ghost_buck();
}

// --- motor cradle ---
// stepped half-bore trough at axle height. putty lines it, taped motor pressed in until
// its face is flush on the inner wall and the shaft centers in the Ø26 opening.
module cradle_right() {
    difference() {
        translate([cradle_x0, -cradle_hw, floor_top-0.1])
            cube([inner_w/2 - cradle_x0 + 0.1, 2*cradle_hw,
                  body_z - floor_top + 0.1]);
        // gearbox bore: Ø37 + 2×2 putty = Ø41, flush to inner wall
        translate([inner_w/2 - gb_len - 1.5, 0, body_z]) rotate([0,90,0])
            cylinder(d=motor_d + 2*putty_gap, h=gb_len + 1.5);
        // motor-can bore: Ø32 + 2×2 putty = Ø36
        translate([cradle_x0 - 1, 0, body_z]) rotate([0,90,0])
            cylinder(d=can_d + 2*putty_gap,
                     h=inner_w/2 - gb_len - cradle_x0 + 2);
        // zip-tie tunnels (under motor) + top edge notches (tie seats)
        for (zx = zip_x) {
            translate([zx - zip_w/2, -cradle_hw - 1, floor_top - 0.2])
                cube([zip_w, 2*cradle_hw + 2, 4.2]);
            translate([zx - zip_w/2, -cradle_hw - 1, body_z - 3])
                cube([zip_w, 2*cradle_hw + 2, 3.5]);
        }
    }
}

// --- tier 0: tub ---
module tub() {
    difference() {
        union() {
            // shell
            difference() {
                translate([0,0,floor_z]) linear_extrude(deck_z - floor_z)
                    footprint();
                translate([0,0,floor_top]) linear_extrude(deck_z)
                    offset(delta=-wall) footprint();
            }
            // deck screw bosses, 4 corners
            for (sx=[-1,1], y=tub_boss_y)
                translate([sx*(inner_w/2 - 2), y, floor_top])
                    cylinder(d=11, h=deck_z - floor_top);
            // motor cradles (mirrored)
            for (sx=[0,1]) mirror([sx,0,0]) cradle_right();
        }
        // top rim chamfer
        difference() {
            translate([0,0,deck_z-2.5]) linear_extrude(3.6) footprint();
            hull() {
                translate([0,0,deck_z-2.51]) linear_extrude(0.01) footprint();
                translate([0,0,deck_z+1.1]) linear_extrude(0.01)
                    offset(delta=-3.6) footprint();
            }
        }
        // wheel shaft openings, Ø26 generous window for boss + shaft
        for (sx=[-1,1]) translate([sx*(inner_w/2 - 1), 0, axle_z])
            rotate([0, sx*90, 0]) cylinder(d=26, h=wall+4);
        // wheel arch scallops
        for (sx=[-1,1]) translate([sx*(half_w+wheel_w/2-1.5), 0, axle_z])
            rotate([0, 90, 0]) cylinder(d=wheel_d+14, h=wheel_w, center=true);
        // caster: Ø28 ball hole + 2x Ø3.4 M3 holes @ 40mm c-c, nuts underneath
        translate([0, caster_y, floor_z-1])
            cylinder(d=cast_dome_hole, h=floor_t+2);
        for (sx=[-1,1])
            translate([sx*(cast_hole_cc/2), caster_y, floor_z-1])
                cylinder(d=m3_clear, h=floor_t+2);
        // floor vents, under BTS drivers (front), LVC (left-mid), ecookie (rear)
        for (sx=[-1,1])
            translate([sx*72, 32, floor_z-1])
                linear_extrude(floor_t+2) vent_row(6);
        for (sx=[-1,1])
            translate([sx*65, -85, floor_z-1])
                linear_extrude(floor_t+2) vent_row(2);
        for (sx=[-1,1])
            translate([sx*55, -138, floor_z-1])
                linear_extrude(floor_t+2) vent_row(3);
        // side vents (driver airflow), clear of shaft openings
        for (sx=[-1,1], y=[-70, 38])
            translate([sx*(inner_w/2+wall/2), y, 26]) rotate([90,0,90])
                linear_extrude(wall+2, center=true)
                    for (i=[0:2]) translate([0, i*8]) slot(26, 4);
        // rear charging / XT60 pass-through
        translate([0, tail_y+wall/2, 46]) rotate([90,0,0])
            linear_extrude(wall+4, center=true) slot(24, 12);
        // deck screw pilot holes (4 corner bosses)
        for (sx=[-1,1], y=tub_boss_y)
            translate([sx*(inner_w/2 - 2), y, deck_z-14]) cylinder(d=m3_pilot, h=15);
    }
}

// --- tier 1: battery deck (printed, don't reprint) ---
module deck() {
    difference() {
        union() {
            translate([-deck_x/2, deck_y0, deck_z])
                cube([deck_x, deck_y1-deck_y0, deck_t]);
            translate([0, bat_off_y, deck_z+deck_t-0.1]) difference() {
                linear_extrude(tray_h)
                    difference() {
                        rrect(bat_l+2*bat_tray_clr+2*tray_wall, bat_w+2*bat_tray_clr+2*tray_wall, 2);
                        rrect(bat_l+2*bat_tray_clr, bat_w+2*bat_tray_clr, 1);
                    }
                for (sx=[-1,1], y=concat(boss_y, leg_y))
                    translate([sx*(inner_w/2 - 2), y-bat_off_y, -1])
                        cube([16, 24, tray_h+2], center=true);
                translate([0, bat_w/2 + bat_tray_clr + tray_wall, tray_h-8])
                    cube([26, 10, 10], center=true);
            }
        }
        for (sx=[-1,1], y=boss_y)
            translate([sx*(inner_w/2 - 2), y, deck_z-1]) {
                cylinder(d=m3_clear, h=deck_t+2);
                translate([0,0,deck_t-0.9]) cylinder(d1=m3_clear, d2=6.6, h=2);
            }
        for (sx=[-1,1], y=leg_y, ly=[-11,11])
            translate([sx*(inner_w/2 - 2), y+ly, deck_z-1]) {
                cylinder(d=m3_clear, h=deck_t+2);
                rotate([0,0,30]) cylinder(d=m3_nut_af, h=2.6, $fn=6);
            }
        for (sx=[-1,1], y=[bat_off_y-42, bat_off_y+58])
            translate([sx*(bat_l/2-6), y, deck_z-1])
                linear_extrude(deck_t+2) rotate([0,0,90]) slot(30, 5);
        translate([0, 18, deck_z-1]) linear_extrude(deck_t+2) slot(48, 18);
        translate([0, -12, deck_z-1]) linear_extrude(deck_t+2) slot(36, 14);
        translate([0, bat_off_y - 55, deck_z-1]) linear_extrude(deck_t+2) slot(52, 20);
        translate([-55, bat_off_y + 35, deck_z-1]) linear_extrude(deck_t+2) slot(16, 14);
        for (y=[bat_off_y-78, bat_off_y+72], sx=[-1,1])
            translate([sx*48, y, deck_z-1])
                linear_extrude(deck_t+2) vent_row(2, l=30, w=4, pitch=9);
    }
}

// honeycomb cutter, interior only - the perimeter band stays solid for the skirt joint
// and the four ±108 wall screws
module hexcut() {
    gcy = (mobo_y0 + mobo_y1)/2;
    xlim = deck_x/2 - hex_band;
    for (i = [-9:9]) {
        cx = i * hex_dx;
        for (j = [-9:9]) {
            cyj = gcy + j*hex_dy + (i % 2 == 0 ? 0 : hex_dy/2);
            if (cx - hex_r >= -xlim && cx + hex_r <= xlim &&
                cyj - hex_af/2 >= mobo_y0 + hex_band &&
                cyj + hex_af/2 <= mobo_y1 - hex_band)
                translate([cx, cyj, mobo_deck_z - 1])
                    cylinder(r=hex_r, h=mobo_deck_t + 2, $fn=6);
        }
    }
}

// --- standoff puck (print 6, 8 for spares) ---
// build: screw a brass M3 standoff into each real board hole, thread the stud into a puck,
// pack the puck cavity with epoxy putty, lower the whole board onto the grid and press.
// putty squeezes through the cells into a mushroom head = a rivet, not a glue joint.
// print top-face-down, only bridge is the pilot roof.
module mobopuck() {
    pr = puck_af / sqrt(3);   // circumradius of the hex body
    difference() {
        cylinder(r=pr, h=puck_h, $fn=6);
        // putty cavity, open at the bottom
        difference() {
            translate([0,0,-1]) cylinder(d=puck_cav_d, h=puck_cav_h+1);
            // central boss carries the pilot; floor sits 1mm proud so it never drops into a cell
            translate([0,0,1]) cylinder(d=puck_boss_d, h=puck_cav_h);
        }
        // M3 pilot for the standoff's male stud
        translate([0,0,1.5]) cylinder(d=puck_pilot, h=puck_h);
        // top chamfer
        translate([0,0,puck_h-0.6])
            cylinder(r1=pr-0.6, r2=pr+1, h=1.2, $fn=6);
    }
}

module mobopuck_tray(n=8) {
    for (i = [0:n-1])
        translate([(i%4)*22, floor(i/4)*22, 0]) mobopuck();
}

// --- T-slot rail (mechanical alternative to the pucks) ---
// bolt 2-3 rails across the honeycomb in X at whatever Y the board's hole rows land on.
// a standoff slides continuously along the rail so only the row has to be eyeballed.
// section from underside: 0-2.2 floor, 2.2-5.5 nut channel (7.2 wide, nut can't rotate),
// 5.5-8.5 cap with a 4.0 slot. 1.6mm overhang per side prints without support.
// needs a 6mm stud (5.4 engaged).
rail_len    = 200;    // < the 201.8 mm honeycomb span
rail_core   = 14.0;   // T-slot body width
rail_flange = 7.0;    // mounting flange each side
rail_w      = rail_core + 2*rail_flange;   // 28
rail_floor  = 2.2;
rail_chan_h = 3.3;
rail_cap    = 3.0;
rail_h      = rail_floor + rail_chan_h + rail_cap;   // 8.5
rail_chan_w = 7.2;    // M3 nut across-flats 7.0 + slop
rail_slot_w = 4.0;    // M3 clearance + slide slop
rail_hole_p = 11.0;   // flange hole pitch, deliberately incommensurate with the 13.42 rib pitch
mobo_clear_rail = rail_h + brass_h;   // 14.5 mm PCB underside clearance

module moborail(len = rail_len) {
    difference() {
        union() {
            // flanged floor
            translate([-len/2, -rail_w/2, 0]) cube([len, rail_w, rail_floor]);
            // T-slot body
            translate([-len/2, -rail_core/2, 0]) cube([len, rail_core, rail_h]);
        }
        // nut channel, open at both ends so nuts slide in
        translate([-len/2-1, -rail_chan_w/2, rail_floor])
            cube([len+2, rail_chan_w, rail_chan_h]);
        // top slot
        translate([-len/2-1, -rail_slot_w/2, rail_floor+rail_chan_h-0.1])
            cube([len+2, rail_slot_w, rail_cap+1]);
        // flange holes, staggered L/R. $fn=24 on purpose: 19 holes x 3 rails at $fn=64
~// pushed the tray STL to 2.5 MB for a clearance hole nothing registers against
        for (i = [-floor(len/2/rail_hole_p) : floor(len/2/rail_hole_p)])
            translate([i*rail_hole_p,
                       (i % 2 == 0 ? 1 : -1) * (rail_core/2 + rail_flange/2),
                       -1])
                cylinder(d=m3_clear, h=rail_floor+2, $fn=24);
        // lead-in chamfers at the channel mouths
        for (sx = [-1, 1])
            translate([sx*len/2, 0, rail_floor + rail_chan_h/2])
                rotate([0, sx*90, 0])
                    cylinder(d1=rail_chan_w+2, d2=rail_chan_w-1, h=2.2, $fn=32);
    }
}

// L-rail: same T-slot but flange on one side only. side=+1 flange on +Y, -1 on -Y.
// mirror in the slicer for the other hand.
module moborail_L(len = rail_len, side = 1) {
    difference() {
        union() {
            // floor under core
            translate([-len/2, -rail_core/2, 0]) cube([len, rail_core, rail_floor]);
            // single-side mounting flange
            translate([-len/2,
                       side > 0 ? rail_core/2 : -rail_core/2 - rail_flange,
                       0])
                cube([len, rail_flange, rail_floor]);
            // T-slot body
            translate([-len/2, -rail_core/2, 0]) cube([len, rail_core, rail_h]);
        }
        translate([-len/2-1, -rail_chan_w/2, rail_floor])
            cube([len+2, rail_chan_w, rail_chan_h]);
        translate([-len/2-1, -rail_slot_w/2, rail_floor+rail_chan_h-0.1])
            cube([len+2, rail_slot_w, rail_cap+1]);
        // holes only on the flanged side
        for (i = [-floor(len/2/rail_hole_p) : floor(len/2/rail_hole_p)])
            translate([i*rail_hole_p,
                       side * (rail_core/2 + rail_flange/2),
                       -1])
                cylinder(d=m3_clear, h=rail_floor+2, $fn=24);
        for (sx = [-1, 1])
            translate([sx*len/2, 0, rail_floor + rail_chan_h/2])
                rotate([0, sx*90, 0])
                    cylinder(d1=rail_chan_w+2, d2=rail_chan_w-1, h=2.2, $fn=32);
    }
}

// spanner washer, goes under the honeycomb at every rail bolt. a rib is only 3.5mm wide
// so a bare M3 head would tear through; 18 af > 13.86 cell circumdia so it always bears
// on rib. a Ø15+ steel fender washer does the same job.
washer_af = 18.0;
washer_t  = 2.5;
module mobowasher() {
    difference() {
        cylinder(r=washer_af/sqrt(3), h=washer_t, $fn=6);
        translate([0,0,-1]) cylinder(d=m3_clear, h=washer_t+2);
    }
}
module mobowasher_tray(n=10) {
    for (i = [0:n-1])
        translate([(i%5)*24, floor(i/5)*22, 0]) mobowasher();
}

// --- tier 2: motherboard deck (honeycomb plate, floating pucks) ---
module mobodeck() {
    cy = (mobo_y0 + mobo_y1)/2;
    skirt_y0 = mobo_y0;
    skirt_y1 = mobo_y1;
    difference() {
        union() {
            // plate, honeycombed below, solid band at the perimeter
            translate([-deck_x/2, mobo_y0, mobo_deck_z])
                cube([deck_x, mobo_y1-mobo_y0, mobo_deck_t]);
            // skirt, U-shape: left, right, front. I/O rear wall omitted for port access.
            translate([0, 0, mobo_deck_z+mobo_deck_t-1])
                linear_extrude(skirt_h+1) difference() {
                    translate([0, cy]) rrect(deck_x, skirt_y1-skirt_y0, 2);
                    translate([0, cy]) rrect(deck_x-8, skirt_y1-skirt_y0-8, 2);
                    // remove rear (I/O) wall segment, floor stays
                    translate([-deck_x/2-2, skirt_y0-2])
                        square([deck_x+4, 10]);
                }
            // (no standoffs, see mobopuck())
        }
        // honeycomb
        hexcut();
        // cooler chimney (top of skirt only)
        translate([0, bat_off_y + 10, mobo_deck_z+mobo_deck_t+skirt_h-1])
            cylinder(d=100, h=6);
        // side skirt vents (left/right walls only)
        for (sx=[-1,1], zz=[12, 24])
            translate([sx*(deck_x/2+2), cy, mobo_deck_z+mobo_deck_t+zz])
                rotate([90,0,90]) linear_extrude(8, center=true)
                    for (i=[-2:2]) translate([i*32, 0]) slot(24, 4);
        // no cable grommets, the 12mm hex cells pass wiring anywhere
        // PicoPSU DC jack access (left skirt)
        translate([-deck_x/2 - 1, bat_off_y + 35, mobo_deck_z + mobo_deck_t + 10])
            rotate([0, 90, 0]) cylinder(d=8, h=wall+4, center=true);
        // plate → wall top screws
        for (sx=[-1,1], y=leg_y)
            translate([sx*wall_x, y, mobo_deck_z-4])
                cylinder(d=m3_clear, h=mobo_deck_t+8);
    }
}

// side walls: bolt to battery deck leg_y pockets, screw up into the plate.
// side=+1 right, -1 left. front/rear open; lift the unit for battery access.
module mobowall(side=1) {
    x0 = side * wall_x;
    y0 = min(leg_y);
    y1 = max(leg_y);
    web_len = y1 - y0 + 28;
    flange_in = 14;
    difference() {
        union() {
            // vertical web centered on old column X (±wall_x)
            translate([x0 - wall_t/2, y0 - 14, deck_z+deck_t])
                cube([wall_t, web_len, wall_h]);
            // bottom flange toward battery, holes hit deck hex pockets
            translate([
                side > 0 ? x0 - wall_t/2 - flange_in : x0 - wall_t/2,
                y0 - 14,
                deck_z+deck_t
            ])
                cube([wall_t + flange_in, web_len, 5]);
            // top pads under plate screws
            for (y=leg_y)
                translate([x0 - wall_t/2, y - 8, mobo_deck_z - 5])
                    cube([wall_t, 16, 5]);
        }
        for (y=leg_y, ly=[-wall_foot_ly, wall_foot_ly])
            translate([x0, y+ly, deck_z+deck_t-1])
                cylinder(d=m3_clear, h=10);
        for (y=leg_y)
            translate([x0, y, mobo_deck_z-12])
                cylinder(d=standoff_pilot, h=14);
        for (zz=[30, 55, 80, 105])
            translate([x0, (y0+y1)/2, deck_z+deck_t+zz])
                rotate([0, 90, 0]) cylinder(d=6, h=wall_t+6, center=true);
    }
}

// --- wheels: PETG hub + TPU tire (printed, don't reprint) ---
lip_od        = 71;
rim_od        = 65;
tire_id       = 63.5;
tire_h        = wheel_w - 6.4;
tread_depth   = 5.0;
lug_rows      = 3;
lugs_per_row  = 14;
row_groove_z  = 2.0;
rad_groove_w  = 3.8;
lug_z         = (tire_h - (lug_rows - 1) * row_groove_z) / lug_rows;
lug_gap_deg   = 360 / lugs_per_row;

module hub() {
    difference() {
        union() {
            cylinder(d=lip_od, h=3);
            translate([0,0,3]) cylinder(d=rim_od, h=wheel_w-6);
            translate([0,0,wheel_w-3]) cylinder(d=lip_od, h=3);
            cylinder(d=24, h=wheel_w);
            for (a=[0:72:288]) rotate([0,0,a])
                translate([-3, 0, 0]) cube([6, rim_od/2-2, wheel_w]);
        }
        translate([0,0,-1]) intersection() {
            cylinder(d=shaft_d+0.25, h=shaft_use+4.5);
            translate([-(shaft_d+2)/2, -(shaft_d+0.25)/2, 0])
                cube([shaft_d+2, shaft_flat+0.25, shaft_use+4.5]);
        }
        translate([0,0,-0.5]) cylinder(d=hex_clr_d, h=3.5);
        translate([0,0,8]) rotate([0,-90,0]) {
            cylinder(d=m3_pilot, h=16);
            translate([0,0,13]) cylinder(d=7, h=20);
        }
        for (a=[36:72:324]) rotate([0,0,a])
            translate([0, 21.5, -1]) cylinder(d=11, h=wheel_w+2);
    }
}

module tire_row_groove(z) {
    translate([0, 0, z])
        rotate_extrude($fn=72)
            translate([wheel_d/2 - tread_depth * 0.65, 0])
                square([tread_depth * 1.35, row_groove_z], center=true);
}

module tire_rad_groove(angle, z_center) {
    rotate([0, 0, angle])
        translate([wheel_d/2 - tread_depth / 2, 0, z_center])
            cube([tread_depth + 2.5, rad_groove_w, lug_z + 0.6], center=true);
}

module tire() {
    difference() {
        cylinder(d=wheel_d, h=tire_h);
        translate([0, 0, -1]) cylinder(d=tire_id, h=tire_h + 2);
        for (row = [1 : lug_rows - 1])
            tire_row_groove(row * lug_z + (row - 0.5) * row_groove_z);
        for (row = [0 : lug_rows - 1], i = [0 : lugs_per_row - 1]) {
            a = i * lug_gap_deg + (row % 2) * lug_gap_deg / 2;
            zc = row * (lug_z + row_groove_z) + lug_z / 2;
            tire_rad_groove(a + lug_gap_deg / 2, zc);
        }
    }
}

// --- caster leveling spacer, print 1 ---
// 80mm wheels put the floor top at 13, caster face needs to be at 20 -> 7mm spacer.
// M3x16 bolts: plate (2.2) + spacer (7) + floor (4) + nut.
module spacer() {
    difference() {
        linear_extrude(spacer_h) rrect(cast_plate_l + 6, cast_plate_w + 4, 3);
        translate([0, 0, -1]) cylinder(d=cast_dome_hole, h=spacer_h+2);
        for (sx=[-1,1])
            translate([sx*(cast_hole_cc/2), 0, -1])
                cylinder(d=m3_clear, h=spacer_h+2);
    }
}

// --- assembly views ---
module assembly_wheels() {
    for (sx=[-1,1]) translate([sx*(half_w+1), 0, axle_z]) rotate([0, sx*90, 0]) {
        color("WhiteSmoke") hub();
        color("DimGray") translate([0,0,3.2]) tire();
    }
}

module assembly_full() {
    color("SteelBlue")   tub();
    color("DarkOrange")  deck();
    color("SeaGreen")    mobodeck();
    color("Olive")       { mobowall(+1); mobowall(-1); }
    tub_electronics();
    ghost_battery();
    ghost_mobo();
    ghost_motors();
    ghost_caster();
    assembly_wheels();
}

module assembly_tub_only() {
    color("SteelBlue") tub();
    tub_electronics();
    ghost_motors();
    ghost_caster();
    assembly_wheels();
}

module assembly_deck_only() {
    color("DarkOrange") deck();
    ghost_battery();
    color("Khaki", 0.6) for (sx=[-1,1], y=[bat_off_y-42, bat_off_y+58])
        translate([sx*(bat_l/2+8), y, deck_z+deck_t+bat_h+5])
            rotate([0, 90, 0]) scale([1, 0.15, 0.15])
                cylinder(d=30, h=120, center=true);
}

module assembly_mobodeck_only() {
    color("SeaGreen") mobodeck();
    color("Olive") { mobowall(+1); mobowall(-1); }
    ghost_mobo();
}

// --- output ---
if (part == "tub")              translate([0,0,-floor_z]) tub();
else if (part == "deck")        translate([0,0,-deck_z]) deck();
else if (part == "mobodeck")    translate([0,0,-mobo_deck_z]) mobodeck();
else if (part == "mobowall_l")  translate([0,0,-(deck_z+deck_t)]) mobowall(-1);
else if (part == "mobowall_r")  translate([0,0,-(deck_z+deck_t)]) mobowall(+1);
else if (part == "mobopuck")    mobopuck();
else if (part == "mobopucks")   mobopuck_tray(8);
else if (part == "moborail")    moborail();
else if (part == "moborails")   for (i=[0:2]) translate([0, i*32, 0]) moborail();
else if (part == "moborail_L")  moborail_L();
else if (part == "moborails_L") for (i=[0:2]) translate([0, i*24, 0]) moborail_L();
else if (part == "mobowasher")  mobowasher();
else if (part == "mobowashers") mobowasher_tray(10);
else if (part == "hub")         hub();
else if (part == "tire")        tire();
else if (part == "spacer")      spacer();
else if (part == "assembly_tub")     assembly_tub_only();
else if (part == "assembly_deck")    assembly_deck_only();
else if (part == "assembly_mobodeck") assembly_mobodeck_only();
else if (part == "assembly")         assembly_full();
else echo(str("ERROR: unknown part='", part, "', export aborted (no geometry)"));
