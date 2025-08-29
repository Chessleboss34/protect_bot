# ============================================================
#  BOT PROTECT - start.py
#  Un seul fichier, multi-serveurs, config JSON, help paginé
# ============================================================

import os
import re
import json
import aiohttp
import asyncio
import datetime
from collections import deque, defaultdict
from keep_alive import keep_alive

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ============================================================
#  [CORE] Chargement .env / Token
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant dans .env")

# ============================================================
#  [CORE] Intents / Bot / Prefix dynamique par serveur
# ============================================================
CONFIG_PATH = "config.json"
_config_lock = asyncio.Lock()

def _read_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def _write_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = _read_config()

def ensure_guild_conf(gid: int):
    gid = str(gid)
    if gid not in config:
        config[gid] = {
            "prefix": "+",
            "log_channel": None,
            "mute_role": None,
            "autorole": None,
            "protect": {
                "antilink": False,
                "link_whitelist": [],  # domains
                "antispam": {
                    "enabled": True,
                    "window_sec": 6,
                    "threshold": 6,
                    "timeout_sec": 300
                },
                "antiraid": {
                    "enabled": False,
                    "window_sec": 60,
                    "max_joins": 8,
                    "action": "lockdown",  # lockdown / log
                    "cooldown_sec": 300
                },
                "antimention": {
                    "enabled": False,
                    "max_mentions": 6
                },
                "antiemoji": {
                    "enabled": False,
                    "max_emojis": 15
                },
                "antiwebhook": True
            },
            "whitelist": [],
            "blacklist": []
        }

for g in list(config.keys()):
    # normalise & ensure structure
    try:
        int(g)
    except:
        continue
    ensure_guild_conf(int(g))

_write_config(config)

async def get_prefix(bot, message):
    if not message.guild:
        return "+"
    gid = str(message.guild.id)
    ensure_guild_conf(message.guild.id)
    return config[gid].get("prefix", "+")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# ============================================================
#  [STATE] Mémoire runtime (anti-spam / anti-raid / cache)
# ============================================================
# Anti-spam: messages récents par (guild, user)
recent_msgs = defaultdict(lambda: defaultdict(lambda: deque(maxlen=50)))
# Anti-raid: timestamps de join par guild
recent_joins = defaultdict(lambda: deque(maxlen=200))
# Cooldown antiraid (évite lock répétés)
antiraid_cooldown_until = defaultdict(lambda: datetime.datetime.utcfromtimestamp(0))
# Uptime
started_at = datetime.datetime.utcnow()

# ============================================================
#  [UTILS] Logs / Embeds / Save config / Checks
# ============================================================
async def save_config():
    async with _config_lock:
        _write_config(config)

def now_utc():
    return datetime.datetime.utcnow()

def human_tdelta(td: datetime.timedelta):
    secs = int(td.total_seconds())
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d: parts.append(f"{d}j")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def is_whitelisted(gid, uid):
    ensure_guild_conf(gid)
    return uid in config[str(gid)]["whitelist"]

def is_blacklisted(gid, uid):
    ensure_guild_conf(gid)
    return uid in config[str(gid)]["blacklist"]

async def send_log(guild: discord.Guild, embed: discord.Embed):
    ensure_guild_conf(guild.id)
    ch_id = config[str(guild.id)].get("log_channel")
    if not ch_id: return
    ch = guild.get_channel(ch_id)
    if not ch:
        # essayer fetch
        try:
            ch = await guild.fetch_channel(ch_id)
        except:
            return
    try:
        await ch.send(embed=embed)
    except:
        pass

def base_embed(title=None, desc=None, color=discord.Color.blurple()):
    e = discord.Embed(color=color, timestamp=datetime.datetime.utcnow())
    if title: e.title = title
    if desc: e.description = desc
    return e

async def ensure_mute_role(guild: discord.Guild):
    ensure_guild_conf(guild.id)
    mrole_id = config[str(guild.id)].get("mute_role")
    mrole = None
    if mrole_id:
        mrole = guild.get_role(mrole_id)
    if not mrole:
        # create if not exists
        try:
            mrole = await guild.create_role(name="Muted", reason="Role pour mute")
            for ch in guild.channels:
                try:
                    await ch.set_permissions(mrole, send_messages=False, speak=False, add_reactions=False)
                except:
                    pass
            config[str(guild.id)]["mute_role"] = mrole.id
            await save_config()
        except:
            pass
    return mrole

async def lockdown(guild: discord.Guild, lock: bool):
    # Lock/unlock tous les salons textuels
    changed = 0
    for ch in guild.text_channels:
        overwrites = ch.overwrites_for(guild.default_role)
        if lock:
            if overwrites.send_messages is not False:
                overwrites.send_messages = False
                try:
                    await ch.set_permissions(guild.default_role, overwrite=overwrites, reason="Lockdown")
                    changed += 1
                except: pass
        else:
            if overwrites.send_messages is False:
                overwrites.send_messages = None
                try:
                    await ch.set_permissions(guild.default_role, overwrite=overwrites, reason="Unlockdown")
                    changed += 1
                except: pass
    return changed

def extract_emojis(text: str):
    # comptera : emojis unicode et custom <:name:id>
    custom = re.findall(r"<a?:\w+:\d+>", text)
    # Unicode rough count (heuristic)
    uni = [c for c in text if c in emoji_unidata]
    return len(custom) + len(uni)

# basic unicode emoji set (lightweight heuristic)
emoji_unidata = set()
try:
    import emoji as _emoji
    for em in _emoji.EMOJI_DATA.keys():
        emoji_unidata.add(em)
except Exception:
    # si lib non dispo, on reste minimal
    pass

# ============================================================
#  [EVENTS] Ready / Guild Join / Autorole
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user} | Guilds: {len(bot.guilds)}")
    await bot.change_presence(activity=discord.Game("Protect Mode 🔒"))
    health_report.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    ensure_guild_conf(guild.id)
    await save_config()
    e = base_embed("Merci de m'avoir ajouté 👋", 
                   f"Utilise `{config[str(guild.id)]['prefix']}setlogs #salon` pour configurer les logs.\nTape `{config[str(guild.id)]['prefix']}help` pour voir toutes les commandes.")
    await send_log(guild, e)

@bot.event
async def on_member_join(member: discord.Member):
    gid = member.guild.id
    ensure_guild_conf(gid)
    # Autorole si configuré
    ar = config[str(gid)].get("autorole")
    if ar:
        role = member.guild.get_role(ar)
        if role:
            try: await member.add_roles(role, reason="Autorole configuré")
            except: pass
    # Logging
    await send_log(member.guild, base_embed("👤 Nouveau membre", f"{member.mention} a rejoint."))
    # Anti-raid
    pr = config[str(gid)]["protect"]["antiraid"]
    if pr["enabled"]:
        now = now_utc()
        recent_joins[gid].append(now)
        # purge fenêtre
        window = datetime.timedelta(seconds=pr["window_sec"])
        while recent_joins[gid] and now - recent_joins[gid][0] > window:
            recent_joins[gid].popleft()
        if len(recent_joins[gid]) >= pr["max_joins"]:
            if now >= antiraid_cooldown_until[gid]:
                action = pr.get("action", "lockdown")
                if action == "lockdown":
                    changed = await lockdown(member.guild, True)
                    await send_log(member.guild, base_embed("🚨 Anti-Raid: LOCKDOWN",
                                                            f"Afflux détecté → `{changed}` salons verrouillés pour {pr['cooldown_sec']}s.",
                                                            discord.Color.red()))
                else:
                    await send_log(member.guild, base_embed("🚨 Anti-Raid",
                                                            f"Afflux détecté (joins={len(recent_joins[gid])}). Action: {action}"))
                antiraid_cooldown_until[gid] = now + datetime.timedelta(seconds=pr["cooldown_sec"])

@tasks.loop(minutes=2)
async def health_report():
    # tâche light pour révoquer lockdown après cooldown si antiraid non actif
    for guild in bot.guilds:
        gid = guild.id
        ensure_guild_conf(gid)
        pr = config[str(gid)]["protect"]["antiraid"]
        if pr["enabled"]:
            # si lockdown a été fait et cooldown écoulé → unlock
            if now_utc() >= antiraid_cooldown_until[gid]:
                # tentons unlock si qlqs salons sont lock (best-effort)
                changed = await lockdown(guild, False)
                if changed:
                    await send_log(guild, base_embed("🔓 Unlockdown", f"{changed} salons déverrouillés.", discord.Color.green()))

# ============================================================
#  [EVENT] Message Create → Anti-link / Anti-spam / Anti-mention / Anti-emoji
# ============================================================
URL_REGEX = re.compile(r"https?://", re.IGNORECASE)

@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return

    gid = message.guild.id
    uid = message.author.id
    ensure_guild_conf(gid)

    # Whitelist / Blacklist (blacklist kick-ban auto)
    if is_blacklisted(gid, uid):
        try:
            await message.author.ban(reason="Blacklist guild")
            await send_log(message.guild, base_embed("⛔ Blacklist",
                                                     f"{message.author} banni automatiquement."))
        except: pass
        return

    # ---- Anti-Link ----
    prot = config[str(gid)]["protect"]
    if prot["antilink"] and not is_whitelisted(gid, uid):
        if URL_REGEX.search(message.content):
            # autoriser si domaine whitelisted
            allowed = False
            for domain in prot["link_whitelist"]:
                if domain.lower() in message.content.lower():
                    allowed = True
                    break
            if not allowed:
                try:
                    await message.delete()
                    await send_log(message.guild, base_embed("🔗 Lien supprimé", f"Par {message.author.mention}"))
                except: pass
                return

    # ---- Anti-Mention ----
    if prot["antimention"]["enabled"] and not is_whitelisted(gid, uid):
        if len(message.mentions) + message.content.count("@") >= prot["antimention"]["max_mentions"]:
            try:
                await message.delete()
            except: pass
            try:
                until = now_utc() + datetime.timedelta(seconds=120)
                await message.author.edit(timed_out_until=until, reason="Anti-mention")
            except: pass
            await send_log(message.guild, base_embed("📣 Anti-mention", f"Message supprimé & timeout léger → {message.author.mention}"))
            return

    # ---- Anti-Emoji Spam ----
    if prot["antiemoji"]["enabled"] and not is_whitelisted(gid, uid):
        if extract_emojis(message.content) >= prot["antiemoji"]["max_emojis"]:
            try:
                await message.delete()
            except: pass
            await send_log(message.guild, base_embed("😵 Anti-emoji", f"Message supprimé → {message.author.mention}"))
            return

    # ---- Anti-Spam ----
    asp = prot["antispam"]
    if asp["enabled"] and not is_whitelisted(gid, uid):
        dq = recent_msgs[gid][uid]
        now = now_utc()
        dq.append(now)
        window = datetime.timedelta(seconds=asp["window_sec"])
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= asp["threshold"]:
            # sanction = timeout
            try:
                until = now + datetime.timedelta(seconds=asp["timeout_sec"])
                await message.author.edit(timed_out_until=until, reason="Anti-spam")
            except: pass
            await send_log(message.guild, base_embed("🚫 Anti-spam", f"{message.author.mention} timeout {asp['timeout_sec']}s"))
            dq.clear()

    await bot.process_commands(message)

# ============================================================
#  [EVENT] Webhooks update → Anti-webhook (log)
# ============================================================
@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel):
    guild = channel.guild
    ensure_guild_conf(guild.id)
    if config[str(guild.id)]["protect"].get("antiwebhook", True):
        await send_log(guild, base_embed("🪝 Webhook modifié", f"Salon: {channel.mention}"))

# ============================================================
#  [HELP] Embeds + Pagination (Boutons)
# ============================================================
class HelpView(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.index = 0

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    prefix = await get_prefix(bot, ctx.message)
    # ---- Pages ----
    p1 = base_embed("🔒 Protect", f"""
`{prefix}setlogs #salon` — définir salon de logs
`{prefix}antilink on/off` — bloque liens (whitelist possible)
`{prefix}linkwhitelist add/remove <domaine>` — gérer domaines autorisés
`{prefix}antispam on/off` — anti-flood
`{prefix}antispam config <window> <threshold> <timeout>` — réglages
`{prefix}antiraid on/off` — anti-raid
`{prefix}antiraid config <window> <max_joins> <action> <cooldown>` — réglages
`{prefix}antimention on/off <max>` — limite @mentions
`{prefix}antiemoji on/off <max>` — limite emojis
`{prefix}whitelist add/remove @user` — bypass protections
`{prefix}blacklist add/remove @user` — ban auto
`{prefix}lock [#ch]` / `{prefix}unlock [#ch]` — verrouille salons
`{prefix}nuke` — recrée le salon courant
`{prefix}autorole set @role` / `clear` — rôle auto à l'arrivée
""", discord.Color.red())

    p2 = base_embed("🛡️ Modération", f"""
`{prefix}ban @user [raison]`
`{prefix}unban <user_id|name#discrim>`
`{prefix}kick @user [raison]`
`{prefix}mute @user [durée]` / `{prefix}unmute @user`
`{prefix}timeout @user <durée>` / `{prefix}untimeout @user`
`{prefix}clear <n>` — purge messages
`{prefix}slowmode <sec>` — mode lent
`{prefix}warn @user [raison]` / `{prefix}warnings @user` / `{prefix}unwarn @user <id>`
`{prefix}nick @user <nouveau>` / `nickreset @user`
`{prefix}role add/remove @user @role`
`{prefix}move @user @vocal` — déplacer en vocal
""", discord.Color.orange())

    p3 = base_embed("⚙️ Admin/Bot", f"""
`{prefix}setname "nom"`
`{prefix}setavatar "url"`
`{prefix}setstatus <playing|watching|listening|streaming> "texte" [url_stream]`
`{prefix}prefix <nouveau>`
`{prefix}serverconfig` — affiche config serveur
`{prefix}setmuterole @role`
`{prefix}exportconfig` — export JSON
`{prefix}importconfig` — répondre avec un fichier JSON
""", discord.Color.blue())

    p4 = base_embed("📊 Utils/Infos", f"""
`{prefix}ping` — latence
`{prefix}uptime`
`{prefix}serverinfo`
`{prefix}userinfo [@user]`
`{prefix}roleinfo @role`
`{prefix}channelinfo [#ch]`
`{prefix}avatar [@user]`
`{prefix}botinfo`
`{prefix}invite`
`{prefix}id` — renvoie les IDs utiles
`{prefix}emojis` — liste emojis du serveur
""", discord.Color.green())

    view = HelpView([p1, p2, p3, p4])
    await ctx.send(embed=p1, view=view)

# ============================================================
#  [ADMIN/BOT CONFIG] prefix / setname / setavatar / setstatus / serverconfig
# ============================================================

# ---- COMMAND: prefix ----
@commands.has_permissions(administrator=True)
@bot.command(name="prefix")
async def prefix_cmd(ctx, new_prefix: str):
    ensure_guild_conf(ctx.guild.id)
    config[str(ctx.guild.id)]["prefix"] = new_prefix
    await save_config()
    await ctx.send(embed=base_embed("✅ Prefix modifié", f"Nouveau préfixe: `{new_prefix}`", discord.Color.green()))

# ---- COMMAND: setname ----
@commands.has_permissions(administrator=True)
@bot.command(name="setname")
async def setname_cmd(ctx, *, name: str):
    try:
        await bot.user.edit(username=name)
        await ctx.send(embed=base_embed("✅ Nom modifié", f"Mon nouveau nom est **{name}**", discord.Color.green()))
    except discord.HTTPException as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ---- COMMAND: setavatar ----
@commands.has_permissions(administrator=True)
@bot.command(name="setavatar")
async def setavatar_cmd(ctx, url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return await ctx.send(embed=base_embed("⚠️ Erreur", "Impossible de télécharger l'image", discord.Color.red()))
            data = await resp.read()
            try:
                await bot.user.edit(avatar=data)
                await ctx.send(embed=base_embed("✅ Avatar modifié", "Nouvelle photo de profil appliquée !", discord.Color.green()))
            except discord.HTTPException as e:
                await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ---- COMMAND: setstatus ----
@commands.has_permissions(administrator=True)
@bot.command(name="setstatus")
async def setstatus_cmd(ctx, status_type: str, *, text_and_url: str):
    # Optionnel: URL pour streaming à la fin
    parts = text_and_url.split()
    url = None
    if status_type.lower() == "streaming" and parts:
        # si dernier token ressemble à une URL on la prend
        if parts[-1].startswith("http"):
            url = parts[-1]
            text = " ".join(parts[:-1]) if len(parts) > 1 else "Live"
        else:
            text = text_and_url
    else:
        text = text_and_url

    st = status_type.lower()
    activity = None
    if st == "playing":
        activity = discord.Game(name=text)
    elif st == "watching":
        activity = discord.Activity(type=discord.ActivityType.watching, name=text)
    elif st == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=text)
    elif st == "streaming":
        activity = discord.Streaming(name=text, url=url or "https://twitch.tv/discord")
    else:
        return await ctx.send(embed=base_embed("⚠️ Type invalide", "Utilise: playing/watching/listening/streaming"))

    await bot.change_presence(activity=activity)
    await ctx.send(embed=base_embed("✅ Statut modifié", f"{status_type} **{text}**"))

# ---- COMMAND: serverconfig ----
@bot.command(name="serverconfig")
@commands.has_permissions(administrator=True)
async def serverconfig_cmd(ctx):
    ensure_guild_conf(ctx.guild.id)
    c = config[str(ctx.guild.id)]
    prot = c["protect"]
    desc = (
        f"**Prefix**: `{c['prefix']}`\n"
        f"**Logs**: {('<#'+str(c['log_channel'])+'>') if c['log_channel'] else 'Non défini'}\n"
        f"**MuteRole**: {('<@&'+str(c['mute_role'])+'>') if c['mute_role'] else 'Auto'}\n"
        f"**Autorole**: {('<@&'+str(c['autorole'])+'>') if c['autorole'] else 'Aucun'}\n"
        f"**AntiLink**: `{prot['antilink']}` | WL: {', '.join(prot['link_whitelist']) if prot['link_whitelist'] else '∅'}\n"
        f"**AntiSpam**: `{prot['antispam']['enabled']}` window={prot['antispam']['window_sec']}s thr={prot['antispam']['threshold']} timeout={prot['antispam']['timeout_sec']}s\n"
        f"**AntiRaid**: `{prot['antiraid']['enabled']}` window={prot['antiraid']['window_sec']}s maxjoins={prot['antiraid']['max_joins']} action={prot['antiraid']['action']} cooldown={prot['antiraid']['cooldown_sec']}s\n"
        f"**AntiMention**: `{prot['antimention']['enabled']}` max={prot['antimention']['max_mentions']}\n"
        f"**AntiEmoji**: `{prot['antiemoji']['enabled']}` max={prot['antiemoji']['max_emojis']}\n"
        f"**AntiWebhook**: `{prot.get('antiwebhook', True)}`\n"
        f"**Whitelist**: {len(c['whitelist'])} | **Blacklist**: {len(c['blacklist'])}\n"
    )
    await ctx.send(embed=base_embed(f"⚙️ Config — {ctx.guild.name}", desc))

# ---- COMMAND: setlogs ----
@bot.command(name="setlogs")
@commands.has_permissions(administrator=True)
async def setlogs_cmd(ctx, channel: discord.TextChannel):
    ensure_guild_conf(ctx.guild.id)
    config[str(ctx.guild.id)]["log_channel"] = channel.id
    await save_config()
    await ctx.send(embed=base_embed("✅ Logs configurés", f"Les logs iront dans {channel.mention}", discord.Color.green()))

# ---- COMMAND: setmuterole ----
@bot.command(name="setmuterole")
@commands.has_permissions(administrator=True)
async def setmuterole_cmd(ctx, role: discord.Role):
    ensure_guild_conf(ctx.guild.id)
    config[str(ctx.guild.id)]["mute_role"] = role.id
    await save_config()
    await ctx.send(embed=base_embed("✅ Rôle mute défini", f"{role.mention}", discord.Color.green()))

# ---- COMMAND: exportconfig / importconfig ----
@bot.command(name="exportconfig")
@commands.has_permissions(administrator=True)
async def exportconfig_cmd(ctx):
    # export config du serveur uniquement
    ensure_guild_conf(ctx.guild.id)
    data = json.dumps(config[str(ctx.guild.id)], indent=2).encode("utf-8")
    file = discord.File(fp=bytes(data), filename=f"config_{ctx.guild.id}.json")
    await ctx.send(embed=base_embed("📦 Export Config", "Voici le JSON de votre config."), file=file)

@bot.command(name="importconfig")
@commands.has_permissions(administrator=True)
async def importconfig_cmd(ctx):
    if not ctx.message.attachments:
        return await ctx.send(embed=base_embed("⚠️ Fichier manquant", "Uploadez un `.json` en pièce jointe."))
    att = ctx.message.attachments[0]
    if not att.filename.endswith(".json"):
        return await ctx.send(embed=base_embed("⚠️ Format invalide", "Fichier attendu: `.json`"))
    try:
        raw = await att.read()
        conf = json.loads(raw.decode("utf-8"))
        ensure_guild_conf(ctx.guild.id)
        config[str(ctx.guild.id)] = conf
        await save_config()
        await ctx.send(embed=base_embed("✅ Import réussi", "Configuration appliquée."))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur import", str(e), discord.Color.red()))

# ============================================================
#  [PROTECT] Anti-link / Whitelist liens
# ============================================================
@bot.command(name="antilink")
@commands.has_permissions(administrator=True)
async def antilink_cmd(ctx, mode: str):
    ensure_guild_conf(ctx.guild.id)
    val = mode.lower() == "on"
    config[str(ctx.guild.id)]["protect"]["antilink"] = val
    await save_config()
    await ctx.send(embed=base_embed("🔗 Anti-link", f"État: `{val}`"))

@bot.command(name="linkwhitelist")
@commands.has_permissions(administrator=True)
async def linkwhitelist_cmd(ctx, sub: str, domain: str = None):
    ensure_guild_conf(ctx.guild.id)
    wl = config[str(ctx.guild.id)]["protect"]["link_whitelist"]
    sub = sub.lower()
    if sub == "add" and domain:
        if domain.lower() not in [d.lower() for d in wl]:
            wl.append(domain)
        await save_config()
        await ctx.send(embed=base_embed("✅ Ajout WL", f"Domaine autorisé: `{domain}`"))
    elif sub == "remove" and domain:
        wl[:] = [d for d in wl if d.lower() != domain.lower()]
        await save_config()
        await ctx.send(embed=base_embed("🗑️ Retrait WL", f"Domaine retiré: `{domain}`"))
    else:
        await ctx.send(embed=base_embed("📄 Whitelist", ", ".join(wl) if wl else "∅"))

# ============================================================
#  [PROTECT] Anti-spam (on/off + config)
# ============================================================
@bot.command(name="antispam")
@commands.has_permissions(administrator=True)
async def antispam_cmd(ctx, mode: str = None):
    ensure_guild_conf(ctx.guild.id)
    asp = config[str(ctx.guild.id)]["protect"]["antispam"]
    if mode is None:
        return await ctx.send(embed=base_embed(
            "🛡️ Anti-spam",
            f"enabled={asp['enabled']} window={asp['window_sec']} thr={asp['threshold']} timeout={asp['timeout_sec']}"
        ))
    val = mode.lower() == "on"
    asp["enabled"] = val
    await save_config()
    await ctx.send(embed=base_embed("🛡️ Anti-spam", f"État: `{val}`"))


@bot.command(name="antispam_config")
@commands.has_permissions(administrator=True)
async def antispam_config_cmd(ctx, window_sec: int, threshold: int, timeout_sec: int):
    ensure_guild_conf(ctx.guild.id)
    asp = config[str(ctx.guild.id)]["protect"]["antispam"]
    asp["window_sec"] = max(2, window_sec)
    asp["threshold"] = max(3, threshold)
    asp["timeout_sec"] = max(10, timeout_sec)
    await save_config()
    await ctx.send(embed=base_embed(
        "⚙️ Anti-spam configuré",
        f"window={asp['window_sec']}s thr={asp['threshold']} timeout={asp['timeout_sec']}s"
    ))

# ============================================================
#  [PROTECT] Anti-raid (on/off + config)
# ============================================================
@bot.command(name="antiraid")
@commands.has_permissions(administrator=True)
async def antiraid_cmd(ctx, mode: str = None):
    ensure_guild_conf(ctx.guild.id)
    ar = config[str(ctx.guild.id)]["protect"]["antiraid"]
    if mode is None:
        return await ctx.send(embed=base_embed("🛡️ Anti-raid", f"enabled={ar['enabled']} window={ar['window_sec']} max_joins={ar['max_joins']} action={ar['action']} cooldown={ar['cooldown_sec']}s"))
    val = mode.lower() == "on"
    ar["enabled"] = val
    await save_config()
    await ctx.send(embed=base_embed("🛡️ Anti-raid", f"État: `{val}`"))

@bot.command(name="antiraid_config")
@commands.has_permissions(administrator=True)
async def antiraid_config_cmd(ctx, window_sec: int, max_joins: int, action: str, cooldown_sec: int):
    ensure_guild_conf(ctx.guild.id)
    ar = config[str(ctx.guild.id)]["protect"]["antiraid"]
    ar["window_sec"] = max(10, window_sec)
    ar["max_joins"] = max(3, max_joins)
    ar["action"] = action if action in ("lockdown", "log") else "lockdown"
    ar["cooldown_sec"] = max(60, cooldown_sec)
    await save_config()
    await ctx.send(embed=base_embed("⚙️ Anti-raid configuré", f"window={ar['window_sec']} max_joins={ar['max_joins']} action={ar['action']} cooldown={ar['cooldown_sec']}s"))

# ============================================================
#  [PROTECT] Anti-mention / Anti-emoji
# ============================================================
@bot.command(name="antimention")
@commands.has_permissions(administrator=True)
async def antimention_cmd(ctx, mode: str, max_mentions: int = None):
    ensure_guild_conf(ctx.guild.id)
    am = config[str(ctx.guild.id)]["protect"]["antimention"]
    am["enabled"] = (mode.lower() == "on")
    if max_mentions is not None:
        am["max_mentions"] = max(2, max_mentions)
    await save_config()
    await ctx.send(embed=base_embed("📣 Anti-mention", f"enabled={am['enabled']} max={am['max_mentions']}"))

@bot.command(name="antiemoji")
@commands.has_permissions(administrator=True)
async def antiemoji_cmd(ctx, mode: str, max_emojis: int = None):
    ensure_guild_conf(ctx.guild.id)
    ae = config[str(ctx.guild.id)]["protect"]["antiemoji"]
    ae["enabled"] = (mode.lower() == "on")
    if max_emojis is not None:
        ae["max_emojis"] = max(5, max_emojis)
    await save_config()
    await ctx.send(embed=base_embed("😵 Anti-emoji", f"enabled={ae['enabled']} max={ae['max_emojis']}"))

# ============================================================
#  [PROTECT] Whitelist / Blacklist
# ============================================================
@bot.command(name="whitelist")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(ctx, sub: str, member: discord.Member = None):
    ensure_guild_conf(ctx.guild.id)
    wl = config[str(ctx.guild.id)]["whitelist"]
    if sub == "add" and member:
        if member.id not in wl: wl.append(member.id)
        await save_config()
        await ctx.send(embed=base_embed("✅ Whitelist", f"{member.mention} ajouté"))
    elif sub == "remove" and member:
        if member.id in wl: wl.remove(member.id)
        await save_config()
        await ctx.send(embed=base_embed("🗑️ Whitelist", f"{member.mention} retiré"))
    else:
        names = []
        for uid in wl:
            u = ctx.guild.get_member(uid)
            names.append(u.mention if u else f"`{uid}`")
        await ctx.send(embed=base_embed("📄 Whitelist", ", ".join(names) if names else "∅"))

@bot.command(name="blacklist")
@commands.has_permissions(administrator=True)
async def blacklist_cmd(ctx, sub: str, member: discord.Member = None):
    ensure_guild_conf(ctx.guild.id)
    bl = config[str(ctx.guild.id)]["blacklist"]
    if sub == "add" and member:
        if member.id not in bl: bl.append(member.id)
        await save_config()
        await ctx.send(embed=base_embed("✅ Blacklist", f"{member.mention} ajouté (sera banni à l'activité)"))
    elif sub == "remove" and member:
        if member.id in bl: bl.remove(member.id)
        await save_config()
        await ctx.send(embed=base_embed("🗑️ Blacklist", f"{member.mention} retiré"))
    else:
        names = []
        for uid in bl:
            u = ctx.guild.get_member(uid)
            names.append(u.mention if u else f"`{uid}`")
        await ctx.send(embed=base_embed("📄 Blacklist", ", ".join(names) if names else "∅"))

# ============================================================
#  [PROTECT] Lock / Unlock / Nuke / Autorole
# ============================================================
@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
async def lock_cmd(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    if ow.send_messages is False:
        return await ctx.send(embed=base_embed("🔒 Lock", f"{ch.mention} est déjà verrouillé"))
    ow.send_messages = False
    try:
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Lock by {ctx.author}")
        await ctx.send(embed=base_embed("🔒 Lock", f"{ch.mention} verrouillé"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock_cmd(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    if ow.send_messages is None:
        return await ctx.send(embed=base_embed("🔓 Unlock", f"{ch.mention} est déjà ouvert"))
    ow.send_messages = None
    try:
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Unlock by {ctx.author}")
        await ctx.send(embed=base_embed("🔓 Unlock", f"{ch.mention} déverrouillé"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="nuke")
@commands.has_permissions(manage_channels=True)
async def nuke_cmd(ctx):
    ch = ctx.channel
    pos = ch.position
    new_ch = await ch.clone(reason=f"Nuke by {ctx.author}")
    await new_ch.edit(position=pos)
    await ch.delete()
    await new_ch.send(embed=base_embed("💥 Nuke", "Salon recréé, messages nettoyés.", discord.Color.red()))

@bot.command(name="autorole")
@commands.has_permissions(manage_roles=True)
async def autorole_cmd(ctx, sub: str, role: discord.Role = None):
    ensure_guild_conf(ctx.guild.id)
    if sub == "set" and role:
        config[str(ctx.guild.id)]["autorole"] = role.id
        await save_config()
        await ctx.send(embed=base_embed("✅ Autorole", f"Rôle défini: {role.mention}"))
    elif sub == "clear":
        config[str(ctx.guild.id)]["autorole"] = None
        await save_config()
        await ctx.send(embed=base_embed("🗑️ Autorole", "Autorole désactivé"))
    else:
        await ctx.send(embed=base_embed("ℹ️ Autorole", "Utilise: `autorole set @role` ou `autorole clear`"))

# ============================================================
#  [MODÉRATION] ban / unban / kick / mute / unmute / timeout / untimeout / clear / slowmode / warn system / nick / role / move
# ============================================================
@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    try:
        await member.ban(reason=f"{reason} | by {ctx.author}")
        await ctx.send(embed=base_embed("✅ Ban", f"{member} banni. Raison: {reason}", discord.Color.red()))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban_cmd(ctx, *, query: str):
    # query peut être ID ou name#discrim
    banned = await ctx.guild.bans()
    target = None
    for e in banned:
        user = e.user
        if str(user.id) == query or f"{user.name}#{user.discriminator}" == query:
            target = user; break
    if not target:
        return await ctx.send(embed=base_embed("❓ Introuvable", query))
    try:
        await ctx.guild.unban(target, reason=f"by {ctx.author}")
        await ctx.send(embed=base_embed("✅ Unban", f"{target} débanni."))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    try:
        await member.kick(reason=f"{reason} | by {ctx.author}")
        await ctx.send(embed=base_embed("✅ Kick", f"{member} expulsé. Raison: {reason}"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ---- Mute via rôle (fallback si timeout indispo) ----
@bot.command(name="mute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def mute_cmd(ctx, member: discord.Member, duration: str = None):
    # essayer timeout si possible
    try:
        if duration:
            # formats: 10s, 5m, 2h
            mult = {"s":1,"m":60,"h":3600}
            unit = duration[-1].lower()
            amount = int(duration[:-1])
            seconds = amount * mult.get(unit, 60)
        else:
            seconds = 600
        until = now_utc() + datetime.timedelta(seconds=seconds)
        await member.edit(timed_out_until=until, reason=f"Mute by {ctx.author}")
        return await ctx.send(embed=base_embed("🔇 Timeout", f"{member.mention} réduit au silence {seconds}s"))
    except:
        # fallback role Mute
        mrole = await ensure_mute_role(ctx.guild)
        if not mrole:
            return await ctx.send(embed=base_embed("⚠️ Erreur", "Impossible de créer/trouver le rôle Muted", discord.Color.red()))
        try:
            await member.add_roles(mrole, reason=f"Mute by {ctx.author}")
            await ctx.send(embed=base_embed("🔇 Mute", f"{member.mention} mute via rôle"))
        except Exception as e:
            await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def unmute_cmd(ctx, member: discord.Member):
    # lever timeout
    try:
        await member.edit(timed_out_until=None, reason=f"Unmute by {ctx.author}")
    except: pass
    # retirer role muted
    mrole_id = config[str(ctx.guild.id)].get("mute_role")
    if mrole_id:
        role = ctx.guild.get_role(mrole_id)
        if role and role in member.roles:
            try: await member.remove_roles(role, reason=f"Unmute by {ctx.author}")
            except: pass
    await ctx.send(embed=base_embed("🔈 Unmute", f"{member.mention} est de nouveau libre."))

@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def timeout_cmd(ctx, member: discord.Member, duration: str):
    mult = {"s":1,"m":60,"h":3600}
    unit = duration[-1].lower()
    amount = int(duration[:-1])
    seconds = amount * mult.get(unit, 60)
    until = now_utc() + datetime.timedelta(seconds=seconds)
    try:
        await member.edit(timed_out_until=until, reason=f"Timeout by {ctx.author}")
        await ctx.send(embed=base_embed("⏳ Timeout", f"{member.mention} → {seconds}s"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="untimeout")
@commands.has_permissions(moderate_members=True)
async def untimeout_cmd(ctx, member: discord.Member):
    try:
        await member.edit(timed_out_until=None, reason=f"untimeout by {ctx.author}")
        await ctx.send(embed=base_embed("✅ Un-timeout", f"{member.mention}"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_cmd(ctx, amount: int):
    try:
        deleted = await ctx.channel.purge(limit=amount+1)
        await ctx.send(embed=base_embed("🧹 Clear", f"{len(deleted)-1} messages supprimés."), delete_after=4)
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode_cmd(ctx, seconds: int):
    try:
        await ctx.channel.edit(slowmode_delay=max(0, seconds))
        await ctx.send(embed=base_embed("🐢 Slowmode", f"{seconds}s"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ---- Warn system (simple en mémoire + logs) ----
warnings_db = defaultdict(lambda: defaultdict(list))  # guild -> user -> [ {id, reason, by, date} ]
_warn_id_seq = 0

@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    global _warn_id_seq
    _warn_id_seq += 1
    rec = {"id": _warn_id_seq, "reason": reason, "by": ctx.author.id, "date": now_utc().isoformat()}
    warnings_db[ctx.guild.id][member.id].append(rec)
    await ctx.send(embed=base_embed("⚠️ Warn", f"{member.mention} — {reason} (id={rec['id']})", discord.Color.orange()))
    await send_log(ctx.guild, base_embed("⚠️ Warn", f"{member} — {reason} (by {ctx.author})", discord.Color.orange()))

@bot.command(name="warnings")
async def warnings_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    lst = warnings_db[ctx.guild.id][member.id]
    if not lst:
        return await ctx.send(embed=base_embed("🗒️ Warnings", "Aucun avertissement."))
    lines = [f"**#{r['id']}** — {r['reason']} (par <@{r['by']}>, {r['date']})" for r in lst]
    await ctx.send(embed=base_embed(f"🗒️ Warnings — {member}", "\n".join(lines)))

@bot.command(name="unwarn")
@commands.has_permissions(moderate_members=True)
async def unwarn_cmd(ctx, member: discord.Member, warn_id: int):
    lst = warnings_db[ctx.guild.id][member.id]
    before = len(lst)
    lst[:] = [r for r in lst if r["id"] != warn_id]
    after = len(lst)
    await ctx.send(embed=base_embed("🗑️ Unwarn", f"Retiré: {before - after}"))

# ---- Nick ----
@bot.command(name="nick")
@commands.has_permissions(manage_nicknames=True)
async def nick_cmd(ctx, member: discord.Member, *, newnick: str):
    try:
        await member.edit(nick=newnick, reason=f"by {ctx.author}")
        await ctx.send(embed=base_embed("✏️ Nick", f"{member.mention} → **{newnick}**"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

@bot.command(name="nickreset")
@commands.has_permissions(manage_nicknames=True)
async def nickreset_cmd(ctx, member: discord.Member):
    try:
        await member.edit(nick=None, reason=f"by {ctx.author}")
        await ctx.send(embed=base_embed("♻️ Nick reset", f"{member.mention}"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ---- Role add/remove ----
@bot.command(name="role")
@commands.has_permissions(manage_roles=True)
async def role_cmd(ctx, sub: str, member: discord.Member, role: discord.Role):
    if sub == "add":
        try:
            await member.add_roles(role, reason=f"by {ctx.author}")
            await ctx.send(embed=base_embed("✅ Role", f"{role.mention} ajouté à {member.mention}"))
        except Exception as e:
            await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))
    elif sub == "remove":
        try:
            await member.remove_roles(role, reason=f"by {ctx.author}")
            await ctx.send(embed=base_embed("🗑️ Role", f"{role.mention} retiré de {member.mention}"))
        except Exception as e:
            await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))
    else:
        await ctx.send(embed=base_embed("ℹ️ Role", "Utilise: `role add @user @role` ou `role remove @user @role`"))

# ---- Move voice ----
@bot.command(name="move")
@commands.has_permissions(move_members=True)
async def move_cmd(ctx, member: discord.Member, channel: discord.VoiceChannel):
    try:
        await member.move_to(channel, reason=f"by {ctx.author}")
        await ctx.send(embed=base_embed("🔊 Move", f"{member.mention} → {channel.mention}"))
    except Exception as e:
        await ctx.send(embed=base_embed("⚠️ Erreur", str(e), discord.Color.red()))

# ============================================================
#  [UTILS] ping / uptime / serverinfo / userinfo / roleinfo / channelinfo / avatar / botinfo / invite / id / emojis
# ============================================================
@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send(embed=base_embed("🏓 Pong", f"{round(bot.latency*1000)}ms"))

@bot.command(name="uptime")
async def uptime_cmd(ctx):
    td = now_utc() - started_at
    await ctx.send(embed=base_embed("⏱️ Uptime", human_tdelta(td)))

@bot.command(name="serverinfo")
async def serverinfo_cmd(ctx):
    g = ctx.guild
    desc = (
        f"**ID:** {g.id}\n"
        f"**Owner:** <@{g.owner_id}>\n"
        f"**Membres:** {g.member_count}\n"
        f"**Salons:** {len(g.channels)} | Text: {len(g.text_channels)} | Voice: {len(g.voice_channels)}\n"
        f"**Rôles:** {len(g.roles)}\n"
        f"**Créé le:** {g.created_at.strftime('%Y-%m-%d')}\n"
    )
    e = base_embed(f"📊 Server Info — {g.name}", desc)
    if g.icon: e.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=e)

@bot.command(name="userinfo")
async def userinfo_cmd(ctx, member: discord.Member = None):
    m = member or ctx.author
    roles = [r.mention for r in m.roles if r != ctx.guild.default_role]
    desc = (
        f"**ID:** {m.id}\n"
        f"**Compte créé:** {m.created_at.strftime('%Y-%m-%d')}\n"
        f"**A rejoint:** {m.joined_at.strftime('%Y-%m-%d') if m.joined_at else 'N/A'}\n"
        f"**Top rôle:** {m.top_role.mention}\n"
        f"**Rôles:** {', '.join(roles) if roles else '∅'}\n"
        f"**Bot:** {m.bot}\n"
    )
    e = base_embed(f"👤 User Info — {m}", desc)
    try:
        e.set_thumbnail(url=m.display_avatar.url)
    except: pass
    await ctx.send(embed=e)

@bot.command(name="roleinfo")
async def roleinfo_cmd(ctx, role: discord.Role):
    perms = ", ".join([p[0] for p in role.permissions if p[1]])[:1000]
    desc = (
        f"**ID:** {role.id}\n"
        f"**Membres:** {len(role.members)}\n"
        f"**Couleur:** {role.color}\n"
        f"**Créé:** {role.created_at.strftime('%Y-%m-%d')}\n"
        f"**Permissions:** {perms if perms else '∅'}\n"
    )
    e = base_embed(f"🏷️ Role Info — {role.name}", desc, role.color or discord.Color.blurple())
    await ctx.send(embed=e)

@bot.command(name="channelinfo")
async def channelinfo_cmd(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    desc = (
        f"**ID:** {ch.id}\n"
        f"**Nom:** {ch.name}\n"
        f"**Créé:** {ch.created_at.strftime('%Y-%m-%d')}\n"
        f"**NSFW:** {getattr(ch, 'nsfw', False)}\n"
        f"**Topic:** {ch.topic or '∅'}\n"
        f"**Slowmode:** {getattr(ch, 'slowmode_delay', 0)}s\n"
    )
    await ctx.send(embed=base_embed(f"🧩 Channel Info — #{ch.name}", desc))

@bot.command(name="avatar")
async def avatar_cmd(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = base_embed(f"🖼️ Avatar — {m}", f"[Ouvrir]({m.display_avatar.url})")
    e.set_image(url=m.display_avatar.url)
    await ctx.send(embed=e)

@bot.command(name="botinfo")
async def botinfo_cmd(ctx):
    g_total = len(bot.guilds)
    users_total = sum(g.member_count for g in bot.guilds)
    td = now_utc() - started_at
    desc = f"**Guilds:** {g_total}\n**Users (approx):** {users_total}\n**Uptime:** {human_tdelta(td)}\n**Latency:** {round(bot.latency*1000)}ms"
    await ctx.send(embed=base_embed("🤖 Bot Info", desc))

@bot.command(name="invite")
async def invite_cmd(ctx):
    perms = "8"  # admin
    url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions={perms}&scope=bot%20applications.commands"
    await ctx.send(embed=base_embed("🔗 Invite", f"[Ajouter le bot]({url})"))

@bot.command(name="id")
async def id_cmd(ctx):
    await ctx.send(embed=base_embed("🆔 IDs", f"Serveur: `{ctx.guild.id}`\nSalon: `{ctx.channel.id}`\nAuteur: `{ctx.author.id}`"))

@bot.command(name="emojis")
async def emojis_cmd(ctx):
    if not ctx.guild.emojis:
        return await ctx.send(embed=base_embed("😀 Emojis", "Aucun emoji."))
    lines = []
    for e in ctx.guild.emojis[:50]:
        lines.append(f"{e} `:{e.name}:` (ID {e.id})")
    await ctx.send(embed=base_embed("😀 Emojis", "\n".join(lines)))


# ============================================================
#  [PROTECT] Gestion du rôle Owner
# ============================================================

OWNER_SUPREME_ID = 123456789012345678  # <-- Remplace par ton ID Discord
OWNER_ROLE_NAME = "Owner"

# Vérifie si un membre est Owner
def is_owner(member: discord.Member):
    role = discord.utils.get(member.roles, name=OWNER_ROLE_NAME)
    return role is not None or member.id == OWNER_SUPREME_ID

# Commande pour donner le rôle Owner à quelqu'un
@bot.command(name="addowner")
async def add_owner_cmd(ctx, member: discord.Member):
    if ctx.author.id != OWNER_SUPREME_ID:
        return await ctx.send(embed=base_embed(
            "❌ Permission refusée",
            "Seul le Owner supreme peut attribuer le rôle Owner."
        ))

    # Cherche le rôle Owner sur le serveur, sinon le crée
    role = discord.utils.get(ctx.guild.roles, name=OWNER_ROLE_NAME)
    if not role:
        role = await ctx.guild.create_role(name=OWNER_ROLE_NAME, permissions=discord.Permissions(administrator=True))
    
    # Ajoute le rôle au membre
    await member.add_roles(role)
    await ctx.send(embed=base_embed(
        "✅ Rôle Owner ajouté",
        f"{member.mention} a reçu le rôle {OWNER_ROLE_NAME}."
    ))

# Décorateur pour protéger une commande avec le rôle Owner
def owner_only():
    async def predicate(ctx):
        if not is_owner(ctx.author):
            raise commands.MissingPermissions(["administrator"])
        return True
    return commands.check(predicate)

# Exemple de commande protégée par Owner
@bot.command(name="secretprotect")
@owner_only()
async def secret_protect_cmd(ctx):
    await ctx.send(embed=base_embed(
        "🔒 Commande Owner",
        "Tu as accès à cette commande spéciale car tu es Owner."
    ))

# ============================================================
#  [QUALITY-OF-LIFE] alias, petites améliorations
# ============================================================
# alias confort
bot.add_command(commands.Command(setstatus_cmd.callback, name="status"))
bot.add_command(commands.Command(setname_cmd.callback, name="rename"))
bot.add_command(commands.Command(clear_cmd.callback, name="purge"))

# ============================================================
#  [RUN] Lancement
# ============================================================
keep_alive()
bot.run(TOKEN)
