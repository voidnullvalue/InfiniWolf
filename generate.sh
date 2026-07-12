#!/bin/sh
set -eu

src_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# When this checkout sits inside the collection's mods directory, install
# straight into the slot run-mod.sh launches from; otherwise keep the
# package local to the checkout. RUN_MOD overrides the launcher lookup.
if [ -d "$src_dir/../installed" ]; then
    mod_dir="$src_dir/../installed/infiniwolf"
else
    mod_dir="$src_dir/infiniwolf"
fi
output="$mod_dir/infiniwolf.pk3"
run_mod=${RUN_MOD:-"$src_dir/../run-mod.sh"}

ask() {
    # ask VAR_NAME PROMPT DEFAULT
    var=$1
    prompt=$2
    default=$3
    printf '%s [%s]: ' "$prompt" "$default"
    read -r reply
    eval "$var=\${reply:-\$default}"
}

ask_dial() {
    # ask_dial VAR_NAME LABEL DEFAULT
    var=$1
    label=$2
    default=$3
    while :; do
        printf '%s (1-5) [%s]: ' "$label" "$default"
        read -r reply
        reply=${reply:-$default}
        case $reply in
            1|2|3|4|5) eval "$var=\$reply"; break ;;
            *) echo "Enter a number 1-5." >&2 ;;
        esac
    done
}

echo "== InfiniWolf campaign generator =="
echo

ask seed "Seed (blank = random each run, or type any word/number for a repeatable one)" ""

ask_dial guard_density   "Guard density"      3
ask_dial enemy_toughness "Enemy toughness"    3
ask_dial supplies        "Ammo/health supply" 3
ask_dial treasure        "Treasure amount"    3
ask_dial secrets         "Secrets per floor"  3
ask_dial locked_doors    "Locked-door gating" 3
ask_dial layout_complexity "Layout complexity" 3

mkdir -p "$mod_dir"

echo
echo "Generating..."

set -- --output "$output" \
    --guard-density "$guard_density" \
    --enemy-toughness "$enemy_toughness" \
    --supplies "$supplies" \
    --treasure "$treasure" \
    --secrets "$secrets" \
    --locked-doors "$locked_doors" \
    --layout-complexity "$layout_complexity"

if [ -n "$seed" ]; then
    set -- --seed "$seed" "$@"
fi

(cd "$src_dir" && python3 -m infiniwolf "$@")

echo "Wrote $output"
echo

if [ ! -x "$run_mod" ]; then
    echo "Launcher not found at $run_mod (set RUN_MOD to your run-mod.sh to enable Play)."
    exit 0
fi

printf 'Play it now? [Y/n]: '
read -r play
case $play in
    ''|[Yy]*)
        # This ECWolf build only looks for its mandatory archive in its
        # startup directory.  Resolve the collection from the launcher (not
        # from this generator or the caller's current directory), then put a
        # relocatable link beside the game data where run-mod.sh starts it.
        run_mod_dir=$(CDPATH= cd -- "$(dirname -- "$run_mod")" && pwd)
        collection_root=$(CDPATH= cd -- "$run_mod_dir/.." && pwd)
        engine_data="$collection_root/share/ecwolf/ecwolf.pk3"
        startup_data="$collection_root/data"
        if [ -f "$engine_data" ] && [ -d "$startup_data" ]; then
            if [ ! -e "$startup_data/ecwolf.pk3" ]; then
                ln -s ../share/ecwolf/ecwolf.pk3 "$startup_data/ecwolf.pk3"
            fi
            if [ ! -r "$startup_data/ecwolf.pk3" ]; then
                echo "Could not stage ECWolf engine data at $startup_data/ecwolf.pk3" >&2
                exit 1
            fi
        fi
        exec "$run_mod" infiniwolf
        ;;
    *) echo "Run '$run_mod infiniwolf' whenever you're ready." ;;
esac
