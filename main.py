import discord
from discord.ext import commands

import asyncio
import os
import logging
import traceback
import sys
import json
import aiohttp
import time
import urllib.parse
import re
import subprocess
import uuid
import math
import numpy as np
from stl import mesh

# ---
# SETUP
# ---

intents = discord.Intents.none()
intents.guild_messages = True
intents.dm_messages = True
intents.message_content = True
intents.guild_reactions = True
intents.guilds = True
logger = logging.getLogger('discord.bot')
base_url = os.getenv('BASE_URL', 'https://vps.suchmeme.nl/print')
openscad_path = os.getenv('OPENSCAD_PATH')
if openscad_path is None:
    raise Exception('OPENSCAD_PATH env var not set')
convert_path = os.getenv('CONVERT_PATH')
if convert_path is None:
    raise Exception('CONVERT_PATH env var not set')
bot_token = os.getenv('BOT_TOKEN')
if bot_token is None:
    raise Exception('BOT_TOKEN env var not set')

react_to_messages = os.getenv("REACT_TO_MESSAGES", "true").lower() == "true"
stl_preview_message = os.getenv("STL_PREVIEW", "true").lower() == "true"
stl_preview_frame_count = int(os.getenv("STL_PREVIEW_FRAME_COUNT", "72"))

class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.print_group = discord.app_commands.Group(name='print', description='Manage the queue for 3d printer files')
        self.tree.add_command(self.print_group)

    async def setup_hook(self):
        await self.tree.sync()

bot = MyClient(intents=intents)

# ---
# CACHE AND STORAGE 
# ---

CHANNEL_MAPPINGS = {}
CACHE = {}

def add_channel_mapping(channel : str, mapping : str):
    global CHANNEL_MAPPINGS
    CHANNEL_MAPPINGS[channel] = mapping

    if not os.path.exists('data'):
        os.mkdir('data')

    with open('data/mappings.json', 'w') as fp:
        json.dump(CHANNEL_MAPPINGS, fp)

def load_channel_mappings():
    global CHANNEL_MAPPINGS

    if not os.path.exists('data/mappings.json'):
        return

    with open('data/mappings.json', 'r') as fp:
        CHANNEL_MAPPINGS = json.load(fp)

load_channel_mappings()

async def get_channel_mapping(channel : str, channel_name : str | None = None) -> str:
    global CHANNEL_MAPPINGS

    if channel not in CHANNEL_MAPPINGS:
        async with aiohttp.ClientSession() as session:
            async with session.post(base_url + '/Saved', json={
                'CollectionName': channel_name if channel_name is not None else channel
            }) as response:
                if response.status != 200:
                    raise Exception('Got non-200 status code in get_channel_mapping()')

                token = await response.text()
                add_channel_mapping(channel, token)

    return CHANNEL_MAPPINGS[channel]

async def get_prints_from_token(token : str, invalidate_cache : bool = False) -> dict:
    global CACHE

    if token in CACHE and not invalidate_cache and (time.time() < (CACHE[token]['time'] + 3600)):
        return CACHE[token]['data']

    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Saved/{token}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    CACHE[token] = {
        'time': time.time(),
        'data': [{
            'uid': x['universalId'],
            'name': x['name'],
            'url': x['website'],
            'image': x['thumbnail']['url'],
            'author': x['author']['name']
        } for x in data['posts'][::-1]]
    }

    return CACHE[token]['data']

async def uid_in_prints_from_token(token : str, uid : str) -> bool:
    items = await get_prints_from_token(token)
    return any(x['uid'] == uid for x in items)

# ---
# Utils
# ---

async def uid_embed(uid : str, color : int = 0x0000FF) -> discord.Embed:
    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Posts/universal/{uid}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    embed = discord.Embed(colour=color, title=data['name'][:60], url=data['website'])
    if data['thumbnail'] is not None or data['thumbnail']['url'] is not None:
        embed.set_image(url=data['thumbnail']['url'])

    embed.set_author(name=data['author']['name'], url=data['author']['website'], icon_url=data['author']['thumbnail']['url'])
    embed.set_footer(text=uid)
    return embed

async def uid_download_embed(uid : str, color : int = 0x00FF00, with_image : bool = False) -> discord.Embed:
    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Posts/universal/{uid}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    def generate_addons(x) -> str:
        addons = []
        if (uid.startswith('prusa-printables:')):
            addons.append(f"([PrusaSlicer]({base_url + '/Hacks/prusa?url=' + urllib.parse.quote(x['url'])}))")

        return ' '.join(addons)

    embed = discord.Embed(colour=color, title=data['name'][:60], url=data['website'])

    if with_image and data['thumbnail'] is not None or data['thumbnail']['url'] is not None:
        embed.set_image(url=data['thumbnail']['url'])

    for x in data['downloads']:
        embed.add_field(name=x['name'], value=f"[Download]({x['url']}) {generate_addons(x)}", inline=False)

    embed.set_author(name=data['author']['name'], url=data['author']['website'], icon_url=data['author']['thumbnail']['url'])
    embed.set_footer(text=uid)
    return embed

def extract_uid(url : str) -> str:
    uid = None

    if url.startswith('prusa-printables:') or url.startswith('thingiverse:') or url.startswith('myminifactory:') or url.startswith("makerworld:"):
        uid = url
    elif url.startswith('https://www.thingiverse.com/thing:'):
        uid = f"thingiverse:{url.split(':')[-1]}"
    elif url.startswith("https://www.myminifactory.com/object/"):
        uid = f"myminifactory:{url.split('-')[-1]}"
    elif url.startswith("https://www.printables.com/model"):
        uid = f"prusa-printables:{url.split('/')[-1].split('-')[0]}"
    elif url.startswith("https://makerworld.com/en/models/"):
        uid = f"makerworld:{url.split('/')[-1].split('#')[0]}"

    return uid

# ---
# Views
# ---

class InteractButton(discord.ui.View):
    def __init__(self, uid : str):
        super().__init__(timeout=86400)
        self.uid = uid

    @discord.ui.button(label='Complete', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.Button):
        await print_complete(interaction, self.uid)

    @discord.ui.button(label='Download', style=discord.ButtonStyle.primary)
    async def list_downloads(self, interaction : discord.Interaction, button : discord.Button):
        await interaction.response.defer(ephemeral=True)
        embed = await uid_download_embed(self.uid)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label='Add to queue', style=discord.ButtonStyle.secondary)
    async def add_to_queue(self, interaction : discord.Interaction, button : discord.Button):
        await print_add(interaction, self.uid)

class DeleteSelf(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Delete', style=discord.ButtonStyle.danger, custom_id="3dprint-msg-delete")
    async def delete(self, interaction: discord.Interaction, button: discord.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.message.delete()

# ---
# Listeners
# ---

DOWNLOAD_SITES = {
    "thingiverse": r"https:\/\/www\.thingiverse\.com\/thing:([0-9]+)[^0-9]*",
    "myminifactory": r"https:\/\/www\.myminifactory\.com\/object\/.*-([0-9]+)[^0-9-]?",
    "prusa-printables": r"https:\/\/www\.printables\.com\/model\/([0-9]+)[^0-9]?",
    "makerworld": r"https:\/\/makerworld\.com\/en\/models\/([0-9]+)[^0-9]?"
}

@bot.event
async def on_message(msg : discord.Message):
    if msg.author.bot:
        return
    
    # Very cool code ddddanya wrote
    for attachment in msg.attachments:
        id = str(uuid.uuid4())
        step = math.floor(360 / stl_preview_frame_count)

        try:
            if stl_preview_message and attachment.content_type == "model/stl":
                async with msg.channel.typing():  
                    stl_filename = id + ".stl"
                    await attachment.save(stl_filename)

                    stl_mesh = mesh.Mesh.from_file(stl_filename)
                    geocenter = (stl_mesh.max_ - stl_mesh.min_)/2
                    geocenter_max = stl_mesh.max_
                    scale = 30/max(geocenter[0],geocenter[1],geocenter[2]/0.5)

                    args = [
                        openscad_path,
                        "blank.scad",
                        "-D", f"'scale([{scale},{scale},{scale}]) translate([{-geocenter_max[0]+geocenter[0]},{-geocenter_max[1]+geocenter[1]},{-geocenter_max[2]+geocenter[2]}]) import(\"{stl_filename}\", convexity=3, center=true); $vpr=[60,0,$t*360]'",
                        "--export-format", "png",
                        "-o", id + ".png",
                        "--preview",
                        "--colorscheme", "BeforeDawn",
                        "--projection", "o",
                        "--animate", str(stl_preview_frame_count),
                        "--autocenter", "--viewall", "-q"
                    ]

                    process = await asyncio.create_subprocess_shell(" ".join(args))

                    if (await process.wait()) != 0:
                        raise Exception("Failed to render preview")

                    # Create gif that is always 6 seconds in length
                    args = [convert_path, "-delay", str(int(600/stl_preview_frame_count)), "-loop", "0"]
                    args.extend([f"{id}{str(x).zfill(5)}.png" for x in range(stl_preview_frame_count)])
                    args.append(id + ".gif")
                    process = await asyncio.create_subprocess_exec(*args)
                    if (await process.wait()) != 0:
                        raise Exception("Failed to create gif")

                    await msg.channel.send(file=discord.File(id + ".gif"))
        except Exception as e:
            logger.error(str(e))
            pass
        finally:
            if os.path.exists(id + ".gif"):
                os.remove(id + ".gif")

            for x in range(stl_preview_frame_count):
                path = f"{id}{str(x).zfill(5)}.png"
                if (os.path.exists(path)):
                    os.remove(path)
            
            if os.path.exists(id + ".stl"):
                os.remove(id + ".stl")

    for site, regex in DOWNLOAD_SITES.items():
        match = re.search(regex, msg.content)
        if react_to_messages and match is not None:
            uid = f"{site}:{match.group(1)}"
            await msg.add_reaction("⬇️")
            start_time = time.time()
            while start_time > (time.time() - 60*60*24):
                res = await bot.wait_for("reaction_add", check=lambda reaction, user: not user.bot and msg.id == reaction.message.id and reaction.emoji == "⬇️", timeout=60*60*24)
                if res != None:
                    channel = await res[1].create_dm()
                    embed = await uid_download_embed(uid)
                    await channel.send(embed=embed, view=DeleteSelf())
            
            break


# ---
# Commands
# ---

async def posts_autocomplete(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    items = await get_prints_from_token(token)
    try:
        result = [
            discord.app_commands.Choice(name=x['name'], value=x['uid'])
            for x in items if current.lower() in x['name'].lower()
        ]
    except Exception as e:
        result = []
        logger.error(str(e))

    return result

@bot.print_group.command(name='complete', description='Mark a print as completed')
@discord.app_commands.autocomplete(print_name=posts_autocomplete)
async def print_complete_command(interaction: discord.Interaction, print_name : str, show_in_channel : bool = False):
    await print_complete(interaction, print_name, show_in_channel)

async def print_complete(interaction: discord.Interaction, uid : str, show_in_channel : bool = False):
    await interaction.response.defer(ephemeral=not show_in_channel)
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    
    if not await uid_in_prints_from_token(token, uid):
        await interaction.followup.send('Print not found in queue', ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        async with session.delete(base_url + f'/Saved/{token}/remove', json={
            'UID': uid
        }) as response: 
            if response.status != 200:
                raise Exception('Failed to complete post')

    asyncio.create_task(get_prints_from_token(token, True))

    embed = await uid_embed(uid, 0xFF0000)
    await interaction.followup.send('Print completed, removed from queue', embed=embed, ephemeral=not show_in_channel)

@bot.print_group.command(name='add', description='Add a 3d print URL to the queue')
async def print_add_command(interaction: discord.Interaction, url : str, show_in_channel : bool = False):
    await print_add(interaction, url, show_in_channel)

async def print_add(interaction: discord.Interaction, url : str, show_in_channel : bool = False):
    url = url.strip()
    await interaction.response.defer(ephemeral=not show_in_channel)
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)

    uid = extract_uid(url)

    if uid is None:
        await interaction.followup.send(f'URL was not recognised', ephemeral=True)
        return

    success = True

    async with aiohttp.ClientSession() as session:
        async with session.post(base_url + f'/Saved/{token}/add', json={
            'UID': uid
        }) as response:
            success = response.status == 200
            fail = await response.text()
            if not success:
                logger.error(fail + ' ' + str(response.status))

    asyncio.create_task(get_prints_from_token(token, True))
    view = InteractButton(uid)

    if success:
        embed = await uid_embed(uid)
        if not show_in_channel:
            embed.set_footer(text=f'API Code: {token}')

        await interaction.followup.send('Added print to queue.', embed=embed, view=view, ephemeral=not show_in_channel)
    else:
        await interaction.followup.send(f'Failed! {fail}', ephemeral=True)

@bot.print_group.command(name='list', description='List current 3d print files in queue')
@discord.app_commands.autocomplete(print_name=posts_autocomplete)
async def print_list_command(interaction: discord.Interaction, print_name : str = None, show_in_channel : bool = False):
    await print_list(interaction, print_name, show_in_channel)

async def print_list(interaction: discord.Interaction, uid : str = None, show_in_channel : bool = False):
    await interaction.response.defer(ephemeral=not show_in_channel)
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    items = await get_prints_from_token(token)

    if (uid != None):
        embed = await uid_embed(uid)

        if not show_in_channel:
            embed.set_footer(text=f'API Code: {token}')

        view = InteractButton(uid)
        await interaction.followup.send(embed=embed, view=view, ephemeral=not show_in_channel)
        return

    embed = discord.Embed(title='Queued Items', color=0xFFFF00, description='\n'.join(
        f"- {x['name']}" for x in items
    ))

    if not show_in_channel:
        embed.set_footer(text=f'API Code: {token}')

    await interaction.followup.send(embed=embed, ephemeral=not show_in_channel)

@bot.print_group.command(name='help', description='Shows the help message of this bot')
async def print_help(interaction : discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    embed = discord.Embed(title='3D Print Queue Bot', color=0x00FF00, description='This bot allows you to manage a queue of 3d prints')
    embed.add_field(name='Commands', value='\n'.join(
        f'`/print {x.name}` - {x.description}' for x in bot.print_group.commands
    ), inline=False)

    embed.add_field(name="How to use", value="This bot keeps track of a list of prints for you. You can use this list however you like, using it as a printing queue is just a suggestion.\n- You can add prints to this list with `/print add <url>`\n- You can view the list with `/print list`\n- You can view a specific print with `/print list print_name:<print name>`\n- You can remove a print from the list with `/print complete <print name>`", inline=False)
    embed.add_field(name='API Code', value=f'You can programatically interact with your stored prints via [your API code]({base_url + "/Saved/" + token}).\nNote that anyone can edit your stored prints using this API code.\nDo not share this with others.', inline=False)
    embed.add_field(name='Supported sites', value="Thingiverse, MyMiniFactory, Printables and MakerWorld are supported by this bot. If you want more sites to be added, please see the contact info below.", inline=False)
    embed.add_field(name='Contact', value='If you have any questions, please contact [suchmememanyskill on Discord](https://discord.com/users/249186838592487425).\nThe source code of the bot can be found [on Github](https://github.com/suchmememanyskill/3d-print-queue-discord-bot).', inline=False)
    embed.add_field(name='Stats', value=f"- {len(CHANNEL_MAPPINGS)} users have used this bot\n- {len(CACHE)} lists have been cached", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.print_group.command(name='info', description='Embeds a 3d print url')
async def print_info_command(interaction: discord.Interaction, url : str, show_in_channel : bool = False):
    await print_info(interaction, url, show_in_channel)

async def print_info(interaction: discord.Interaction, url : str, show_in_channel : bool = False):
    url = url.strip()
    await interaction.response.defer(ephemeral=not show_in_channel)

    uid = extract_uid(url)

    if uid is None:
        await interaction.followup.send('URL was not recognised', ephemeral=True)
        return

    embed = await uid_embed(uid)
    view = InteractButton(uid)
    await interaction.followup.send(embed=embed, view=view, ephemeral=not show_in_channel)

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')
    bot.add_view(DeleteSelf())

bot.run(bot_token)
