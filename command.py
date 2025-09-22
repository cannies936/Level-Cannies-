import discord
import random
import re
import os
import asyncio
import glob
from discord.ext import commands
from typing import Optional
from collections import defaultdict, deque
import time

# Discordボット設定
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True  # サーバー参加・退出イベントに必要
intents.members = True  # メンバー情報取得に必要
bot = commands.Bot(command_prefix='n!', intents=intents)

# ロール名の定数
ROLE_NAME = "Level Cannies η"

# スパム対策設定
SPAM_SETTINGS = {
    'message_limit': 5,        # X秒間でのメッセージ数制限
    'time_window': 10,         # 時間窓（秒）
    'duplicate_limit': 3,      # 同一メッセージの連続投稿制限
    'warning_threshold': 2,    # 警告しきい値
    'mute_duration': 300,      # ミュート時間（秒、5分）
    'enabled': True            # スパム対策有効/無効
}

# スパム検出用データ構造（サーバー・ユーザー別にスコープ）
user_message_history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=20)))  # (guild_id, user_id)のメッセージ履歴
user_last_messages = defaultdict(lambda: defaultdict(lambda: deque(maxlen=5)))     # (guild_id, user_id)の最新メッセージ内容
user_warnings = defaultdict(lambda: defaultdict(int))                             # (guild_id, user_id)の警告回数
spam_stats = defaultdict(lambda: {'messages_deleted': 0, 'warnings_given': 0, 'mutes_applied': 0})
pending_unmutes = {}  # 予定されているミュート解除タスクを追跡

# ホワイトリスト用データ構造（サーバー別にスコープ）
def create_whitelist():
    return {
        'users': set(),    # ホワイトリストユーザーのIDセット
        'roles': set(),    # ホワイトリストロールのIDセット
        'enabled': False   # ホワイトリスト機能の有効/無効
    }

whitelist_data = defaultdict(create_whitelist)

# 禁止ワード用データ構造（サーバー別にスコープ）
def create_banword_settings():
    return {
        'words': set(),           # 禁止ワードのセット
        'enabled': False,         # 禁止ワード機能の有効/無効
        'action': 'delete',       # 対処方法 ('delete', 'warn', 'mute')
        'case_sensitive': False   # 大文字小文字を区別するか
    }

banword_data = defaultdict(create_banword_settings)

async def is_spam(message):
    """スパムを検出する関数"""
    if not SPAM_SETTINGS['enabled']:
        return False
    
    user_id = message.author.id
    guild_id = message.guild.id
    current_time = time.time()
    
    # ボットメッセージやコマンドは除外
    if message.author.bot or message.content.startswith('n!'):
        return False
    
    # 管理者は除外
    if message.author.guild_permissions.administrator:
        return False
    
    # メッセージ履歴に追加（サーバー別にスコープ）
    user_message_history[guild_id][user_id].append(current_time)
    user_last_messages[guild_id][user_id].append(message.content.lower().strip())
    
    # 1. 短時間での大量投稿チェック
    recent_messages = [t for t in user_message_history[guild_id][user_id] 
                      if current_time - t <= SPAM_SETTINGS['time_window']]
    
    if len(recent_messages) >= SPAM_SETTINGS['message_limit']:
        return True
    
    # 2. 同一メッセージの連続投稿チェック
    if len(user_last_messages[guild_id][user_id]) >= SPAM_SETTINGS['duplicate_limit']:
        recent_contents = list(user_last_messages[guild_id][user_id])[-SPAM_SETTINGS['duplicate_limit']:]
        if len(set(recent_contents)) == 1 and recent_contents[0].strip():  # 空文字は除外
            return True
    
    return False

def is_whitelisted(member):
    """ユーザーまたはロールがホワイトリストに登録されているかチェック"""
    if not member.guild:
        return False
    
    guild_id = member.guild.id
    whitelist = whitelist_data[guild_id]
    
    # ホワイトリスト機能が無効の場合は常にFalse
    if not whitelist['enabled']:
        return False
    
    # ユーザーIDがホワイトリストに登録されているかチェック
    if member.id in whitelist['users']:
        return True
    
    # ユーザーのロールがホワイトリストに登録されているかチェック
    for role in member.roles:
        if role.id in whitelist['roles']:
            return True
    
    return False

def contains_banned_word(message):
    """メッセージに禁止ワードが含まれているかチェック"""
    if not message.guild:
        return False, None
    
    guild_id = message.guild.id
    banword_settings = banword_data[guild_id]
    
    # 禁止ワード機能が無効の場合は常にFalse
    if not banword_settings['enabled']:
        return False, None
    
    # メッセージ内容を取得
    content = message.content
    if not content:
        return False, None
    
    # 大文字小文字を区別しない場合は小文字に変換
    if not banword_settings['case_sensitive']:
        content = content.lower()
    
    # 各禁止ワードをチェック
    for banned_word in banword_settings['words']:
        check_word = banned_word if banword_settings['case_sensitive'] else banned_word.lower()
        if check_word in content:
            return True, banned_word
    
    return False, None

async def handle_spam_action(message):
    """スパム対処を実行する関数"""
    user_id = message.author.id
    guild = message.guild
    user = message.author
    
    # 警告回数を増加（サーバー別にスコープ）
    user_warnings[guild.id][user_id] += 1
    spam_stats[guild.id]['warnings_given'] += 1
    
    try:
        # メッセージを削除
        await message.delete()
        spam_stats[guild.id]['messages_deleted'] += 1
        
        # 警告レベルに応じた対処
        if user_warnings[guild.id][user_id] >= SPAM_SETTINGS['warning_threshold']:
            # ミュート処理
            try:
                mute_role = discord.utils.get(guild.roles, name="Muted")
                if not mute_role:
                    # Mutedロールを作成
                    mute_role = await guild.create_role(name="Muted", reason="スパム対策用ミュートロール")
                    # 全チャンネルでの発言を禁止
                    for channel in guild.channels:
                        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
                            await channel.set_permissions(mute_role, send_messages=False, speak=False)
                
                await user.add_roles(mute_role, reason=f"スパム行為のため自動ミュート")
                spam_stats[guild.id]['mutes_applied'] += 1
                
                # ミュート解除のタスクを非同期で実行
                async def unmute_after_delay():
                    await asyncio.sleep(SPAM_SETTINGS['mute_duration'])
                    try:
                        await user.remove_roles(mute_role, reason="ミュート期間終了")
                        user_warnings[guild.id][user_id] = 0  # 警告をリセット
                        if (guild.id, user_id) in pending_unmutes:
                            del pending_unmutes[(guild.id, user_id)]
                    except Exception as e:
                        print(f"自動ミュート解除エラー: {e}")
                
                # タスクを作成して追跡
                task = asyncio.create_task(unmute_after_delay())
                pending_unmutes[(guild.id, user_id)] = task
                
            except discord.Forbidden:
                print(f"ミュート権限不足: {user.name} (サーバー: {guild.name})")
            except Exception as e:
                print(f"ミュート処理エラー: {e}")
        else:
            # 警告メッセージ
            try:
                warning_embed = discord.Embed(
                    title="⚠️ スパム警告",
                    description=f"{user.mention} スパム行為が検出されました。\n警告回数: {user_warnings[guild.id][user_id]}/{SPAM_SETTINGS['warning_threshold']}",
                    color=discord.Color.orange()
                )
                warning_embed.add_field(
                    name="注意事項", 
                    value="短時間での大量投稿や同じメッセージの繰り返しはスパムとみなされます。", 
                    inline=False
                )
                await message.channel.send(embed=warning_embed, delete_after=10)
            except Exception as e:
                print(f"警告メッセージ送信エラー: {e}")
                
    except discord.NotFound:
        pass  # メッセージが既に削除されている
    except Exception as e:
        print(f"スパム対処エラー: {e}")

async def handle_banned_word_action(message, banned_word):
    """禁止ワード検出時の対処を実行する関数"""
    if not message.guild:
        return
    
    guild_id = message.guild.id
    banword_settings = banword_data[guild_id]
    action = banword_settings['action']
    
    try:
        if action == 'delete':
            # メッセージを削除
            await message.delete()
            
            # 警告メッセージを送信
            embed = discord.Embed(
                title="🚫 禁止ワード検出",
                description=f"{message.author.mention} 禁止されたワードが検出されました。",
                color=discord.Color.red()
            )
            embed.add_field(
                name="対処", 
                value="メッセージを削除しました。", 
                inline=False
            )
            embed.add_field(
                name="注意事項", 
                value="禁止されたワードを含むメッセージは自動的に削除されます。", 
                inline=False
            )
            
            await message.channel.send(embed=embed, delete_after=10)
            
        elif action == 'warn':
            # 警告のみ（メッセージは削除しない）
            embed = discord.Embed(
                title="⚠️ 禁止ワード警告",
                description=f"{message.author.mention} 禁止されたワードが検出されました。",
                color=discord.Color.orange()
            )
            embed.add_field(
                name="警告", 
                value="不適切な言葉の使用は控えてください。", 
                inline=False
            )
            embed.add_field(
                name="注意事項", 
                value="今後このような言葉の使用は避けてください。", 
                inline=False
            )
            
            await message.channel.send(embed=embed, delete_after=15)
            
        elif action == 'mute':
            # メッセージを削除してユーザーをミュート
            await message.delete()
            
            guild = message.guild
            user = message.author
            
            # Mutedロールを取得または作成
            mute_role = discord.utils.get(guild.roles, name="Muted")
            if not mute_role:
                mute_role = await guild.create_role(name="Muted", reason="禁止ワード対策用ミュートロール")
                # 全チャンネルでの発言を禁止
                for channel in guild.channels:
                    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
                        await channel.set_permissions(mute_role, send_messages=False, speak=False)
            
            await user.add_roles(mute_role, reason=f"禁止ワード使用のため自動ミュート: {banned_word}")
            
            # 警告メッセージを送信
            embed = discord.Embed(
                title="🔇 禁止ワード検出 - ミュート",
                description=f"{user.mention} 禁止されたワードの使用によりミュートされました。",
                color=discord.Color.red()
            )
            embed.add_field(
                name="対処", 
                value="メッセージを削除し、ユーザーを一時的にミュートしました。", 
                inline=False
            )
            embed.add_field(
                name="ミュート解除", 
                value="管理者に解除を依頼するか、一定時間後に自動解除されます。", 
                inline=False
            )
            
            await message.channel.send(embed=embed, delete_after=20)
            
            # 30分後に自動ミュート解除
            async def unmute_after_delay():
                await asyncio.sleep(1800)  # 30分
                try:
                    await user.remove_roles(mute_role, reason="禁止ワード自動ミュート期間終了")
                except Exception as e:
                    print(f"禁止ワード自動ミュート解除エラー: {e}")
            
            asyncio.create_task(unmute_after_delay())
        
        # ログ出力
        print(f"🚫 禁止ワード検出: {banned_word} | 対処: {action} | ユーザー: {message.author} | サーバー: {message.guild.name}")
        
    except discord.NotFound:
        pass  # メッセージが既に削除されている
    except discord.Forbidden:
        print(f"禁止ワード対処権限不足: {message.guild.name}")
    except Exception as e:
        print(f"禁止ワード対処エラー: {e}")

@bot.event
async def on_message(message):
    """メッセージ受信時のイベント"""
    # ボットメッセージは無視
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    # スパム検出
    if message.guild and await is_spam(message):
        await handle_spam_action(message)
        return  # スパムの場合はコマンド処理をスキップ
    
    # 禁止ワード検出
    if message.guild:
        contains_banned, banned_word = contains_banned_word(message)
        if contains_banned:
            await handle_banned_word_action(message, banned_word)
            return  # 禁止ワードの場合はコマンド処理をスキップ
    
    # 通常のコマンド処理
    await bot.process_commands(message)

@bot.event
async def on_ready():
    if bot.user:
        print(f'{bot.user} としてログインしました！')
        print(f'Bot ID: {bot.user.id}')
    print('ボットが準備完了です！')

@bot.event
async def on_guild_join(guild):
    """ボットがサーバーに参加した時のイベント"""
    try:
        # ボット自身を取得（フォールバック付き）
        bot_member = guild.me
        if not bot_member and bot.user:
            try:
                bot_member = await guild.fetch_member(bot.user.id)
            except discord.NotFound:
                print(f"❌ サーバー '{guild.name}' でボット自身が見つかりませんでした")
                return
        
        if not bot_member:
            print(f"❌ サーバー '{guild.name}' でボット情報を取得できませんでした")
            return
        
        # 必要な権限をチェック
        if not bot_member.guild_permissions.manage_roles:
            print(f"❌ サーバー '{guild.name}' でロール管理権限がありません")
            print(f"管理者にロール管理権限の付与を依頼してください")
            return
        
        # 既存のロールをチェック
        existing_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        
        if existing_role:
            # 既存ロールの階層をチェック
            if existing_role >= bot_member.top_role:
                print(f"❌ サーバー '{guild.name}' でロール '{ROLE_NAME}' はボットより上位にあります")
                print(f"管理者にボットのロールを '{ROLE_NAME}' より上に移動してもらってください")
                return
            
            # 既存のロールがある場合は付与
            if existing_role not in bot_member.roles:
                await bot_member.add_roles(existing_role, reason="ボット参加時の自動ロール付与")
                print(f"✅ サーバー '{guild.name}' で既存のロール '{ROLE_NAME}' を付与しました")
            else:
                print(f"✅ サーバー '{guild.name}' でロール '{ROLE_NAME}' は既に付与済みです")
        else:
            # ロールが存在しない場合は作成して付与
            try:
                new_role = await guild.create_role(
                    name=ROLE_NAME,
                    color=discord.Color.blue(),
                    reason="ボット参加時の自動ロール作成"
                )
                
                # 作成したロールをボットより下に配置
                if new_role.position >= bot_member.top_role.position:
                    try:
                        await new_role.edit(position=max(1, bot_member.top_role.position - 1))
                    except discord.HTTPException:
                        print(f"⚠️ サーバー '{guild.name}' でロール位置の調整に失敗しました")
                
                await bot_member.add_roles(new_role, reason="ボット参加時の自動ロール付与")
                print(f"✅ サーバー '{guild.name}' でロール '{ROLE_NAME}' を作成・付与しました")
                
            except discord.Forbidden:
                print(f"❌ サーバー '{guild.name}' でロール作成権限が不足しています")
            except discord.HTTPException as e:
                print(f"❌ サーバー '{guild.name}' でロール作成中にHTTPエラー: {e}")
                
    except discord.Forbidden:
        print(f"❌ サーバー '{guild.name}' で権限が不足しています")
    except Exception as e:
        print(f"❌ サーバー '{guild.name}' 参加時に予期しないエラーが発生: {type(e).__name__}: {e}")

@bot.event
async def on_guild_remove(guild):
    """ボットがサーバーから退出した時のイベント"""
    try:
        # ロールを削除（ボットが退出しているので直接削除はできないが、
        # 他のボットや管理者によって削除される可能性を考慮してログ出力）
        print(f"🚪 サーバー '{guild.name}' から退出しました")
        print(f"注意: ロール '{ROLE_NAME}' が残っている場合は手動で削除してください")
        
    except Exception as e:
        print(f"❌ サーバー '{guild.name}' 退出時にエラーが発生: {e}")

@bot.command(name='ping')
async def ping(ctx):
    """Botの応答時間を確認"""
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')

@bot.command(name='dice')
async def dice_roll(ctx, dice_notation=None):
    """
    サイコロを振るコマンド
    使用例:
    n!dice - 6面ダイスを1個振る
    n!dice 20 - 20面ダイスを1個振る      n!dice 3d6 - 6面ダイスを3個振る
     n!dice 2d20 - 20面ダイスを2個振る
    """
    
    if dice_notation is None:
        # 基本の6面ダイス
        result = random.randint(1, 6)
        await ctx.send(f'🎲 サイコロの結果: **{result}**')
        return
    
    # 数字のみの場合（面数指定）
    if dice_notation.isdigit():
        sides = int(dice_notation)
        if sides < 2:
            await ctx.send('❌ ダイスの面数は2以上である必要があります')
            return
        if sides > 1000:
            await ctx.send('❌ ダイスの面数は1000以下である必要があります')
            return
            
        result = random.randint(1, sides)
        await ctx.send(f'🎲 {sides}面ダイスの結果: **{result}**')
        return
    
    # XdY形式の場合（個数d面数）
    dice_pattern = re.match(r'^(\d+)d(\d+)$', dice_notation.lower())
    if dice_pattern:
        num_dice = int(dice_pattern.group(1))
        sides = int(dice_pattern.group(2))
        
        # 制限チェック
        if num_dice < 1 or num_dice > 20:
            await ctx.send('❌ ダイスの個数は1-20個の範囲で指定してください')
            return
        if sides < 2 or sides > 1000:
            await ctx.send('❌ ダイスの面数は2-1000の範囲で指定してください')
            return
        
        # ダイスを振る
        results = [random.randint(1, sides) for _ in range(num_dice)]
        total = sum(results)
        
        # 結果表示
        results_str = ', '.join(str(r) for r in results)
        if num_dice == 1:
            await ctx.send(f'🎲 {num_dice}d{sides}の結果: **{results[0]}**')
        else:
            await ctx.send(f'🎲 {num_dice}d{sides}の結果: [{results_str}] = **{total}**')
        return
    
    # 無効な形式
    await ctx.send('❌ 無効な形式です。使用例: `n!dice`, `n!dice 20`, `n!dice 3d6`')

@bot.command(name='fizzbuzz')
async def fizzbuzz_game(ctx, number=None):
    """
    FizzBuzzゲーム
    数字を入力すると、3の倍数で「Fizz」、5の倍数で「Buzz」、両方で「FizzBuzz」を表示
    使用例:
    n!fizzbuzz 15 - 15を入力すると「FizzBuzz」
    n!fizzbuzz 9 - 9を入力すると「Fizz」
    n!fizzbuzz 10 - 10を入力すると「Buzz」
    n!fizzbuzz 7 - 7を入力すると「7」
    """
    
    if number is None:
        # ヘルプメッセージ
        embed = discord.Embed(
            title="🎮 FizzBuzzゲーム",
            description="数字を入力してFizzBuzzの結果を確認しよう！",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="ルール",
            value="""
🔢 **3の倍数** → Fizz
🔢 **5の倍数** → Buzz  
🔢 **3と5の倍数** → FizzBuzz
🔢 **それ以外** → 数字をそのまま表示
            """,
            inline=False
        )
        embed.add_field(
            name="使用例",
            value="""
`n!fizzbuzz 15` → FizzBuzz
`n!fizzbuzz 9` → Fizz
`n!fizzbuzz 10` → Buzz
`n!fizzbuzz 7` → 7
            """,
            inline=False
        )
        embed.set_footer(text="数字（1-1000）を指定してゲームを開始してください")
        await ctx.send(embed=embed)
        return
    
    # 数字の入力検証
    try:
        num = int(number)
    except ValueError:
        await ctx.send('❌ 有効な数字を入力してください。\n使用例: `n!fizzbuzz 15`')
        return
    
    # 範囲チェック
    if num < 1:
        await ctx.send('❌ 1以上の数字を入力してください。')
        return
    if num > 1000:
        await ctx.send('❌ 1000以下の数字を入力してください。')
        return
    
    # FizzBuzz判定
    result = ""
    if num % 15 == 0:  # 3と5の両方の倍数
        result = "FizzBuzz"
        color = discord.Color.purple()
        emoji = "🎉"
    elif num % 3 == 0:  # 3の倍数
        result = "Fizz"
        color = discord.Color.green()
        emoji = "🟢"
    elif num % 5 == 0:  # 5の倍数
        result = "Buzz"
        color = discord.Color.orange()
        emoji = "🟠"
    else:  # それ以外
        result = str(num)
        color = discord.Color.blue()
        emoji = "🔢"
    
    # 結果表示
    embed = discord.Embed(
        title=f"{emoji} FizzBuzz結果",
        color=color
    )
    embed.add_field(
        name="入力した数字",
        value=f"**{num}**",
        inline=True
    )
    embed.add_field(
        name="結果",
        value=f"**{result}**",
        inline=True
    )
    
    # 結果の説明を追加
    if result == "FizzBuzz":
        explanation = f"{num}は3と5の両方で割り切れます"
    elif result == "Fizz":
        explanation = f"{num}は3で割り切れます"
    elif result == "Buzz":
        explanation = f"{num}は5で割り切れます"
    else:
        explanation = f"{num}は3でも5でも割り切れません"
    
    embed.add_field(
        name="説明",
        value=explanation,
        inline=False
    )
    
    embed.set_footer(text="別の数字でも試してみてください！")
    
    await ctx.send(embed=embed)

@bot.command(name='whitelist')
@commands.has_permissions(manage_guild=True)
async def whitelist(ctx, action: str = "status", target_type: Optional[str] = None, *, target: Optional[str] = None):
    """
    ホワイトリスト管理コマンド
    使用例:
    n!whitelist status - ホワイトリストの状態を表示
    n!whitelist enable - ホワイトリストを有効にする
    n!whitelist disable - ホワイトリストを無効にする
    n!whitelist add user @ユーザー - ユーザーをホワイトリストに追加
    n!whitelist remove user @ユーザー - ユーザーをホワイトリストから削除
    n!whitelist add role @ロール - ロールをホワイトリストに追加
    n!whitelist remove role @ロール - ロールをホワイトリストから削除
    n!whitelist list - ホワイトリストの内容を表示
    n!whitelist clear - ホワイトリストをクリア
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    guild_id = ctx.guild.id
    whitelist = whitelist_data[guild_id]
    action = action.lower()
    
    try:
        if action == "status":
            # ホワイトリストの現在の状態を表示
            embed = discord.Embed(
                title="📋 ホワイトリストステータス",
                color=discord.Color.green() if whitelist['enabled'] else discord.Color.red()
            )
            
            status = "🟢 有効" if whitelist['enabled'] else "🔴 無効"
            embed.add_field(name="現在の状態", value=status, inline=True)
            
            embed.add_field(name="登録ユーザー数", value=f"{len(whitelist['users'])}人", inline=True)
            embed.add_field(name="登録ロール数", value=f"{len(whitelist['roles'])}個", inline=True)
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action == "enable":
            # ホワイトリストを有効にする
            whitelist['enabled'] = True
            embed = discord.Embed(
                title="✅ ホワイトリスト有効化",
                description="ホワイトリストを有効にしました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        elif action == "disable":
            # ホワイトリストを無効にする
            whitelist['enabled'] = False
            embed = discord.Embed(
                title="🔴 ホワイトリスト無効化",
                description="ホワイトリストを無効にしました。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            
        elif action == "add":
            if not target_type or not target:
                await ctx.send('❌ 追加対象を指定してください。\n使用例: `n!whitelist add user @ユーザー` または `n!whitelist add role @ロール`')
                return
            
            target_type = target_type.lower()
            
            if target_type == "user":
                # ユーザーを追加
                user = None
                if ctx.message.mentions:
                    user = ctx.message.mentions[0]
                else:
                    # IDで検索
                    try:
                        user_id = int(target.strip('<@!>'))
                        user = ctx.guild.get_member(user_id)
                    except ValueError:
                        await ctx.send('❌ 有効なユーザーを指定してください。')
                        return
                
                if not user:
                    await ctx.send('❌ ユーザーが見つかりませんでした。')
                    return
                
                if user.id in whitelist['users']:
                    await ctx.send(f'❌ {user.mention} は既にホワイトリストに登録されています。')
                    return
                
                whitelist['users'].add(user.id)
                embed = discord.Embed(
                    title="✅ ユーザー追加完了",
                    description=f"{user.mention} をホワイトリストに追加しました。",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            elif target_type == "role":
                # ロールを追加
                role = None
                if ctx.message.role_mentions:
                    role = ctx.message.role_mentions[0]
                else:
                    # 名前またはIDで検索
                    role = discord.utils.get(ctx.guild.roles, name=target) or discord.utils.get(ctx.guild.roles, id=int(target) if target.isdigit() else None)
                
                if not role:
                    await ctx.send('❌ ロールが見つかりませんでした。')
                    return
                
                if role.id in whitelist['roles']:
                    await ctx.send(f'❌ {role.mention} は既にホワイトリストに登録されています。')
                    return
                
                whitelist['roles'].add(role.id)
                embed = discord.Embed(
                    title="✅ ロール追加完了",
                    description=f"{role.mention} をホワイトリストに追加しました。",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            else:
                await ctx.send('❌ 無効な対象タイプです。`user` または `role` を指定してください。')
                
        elif action == "remove":
            if not target_type or not target:
                await ctx.send('❌ 削除対象を指定してください。\n使用例: `n!whitelist remove user @ユーザー` または `n!whitelist remove role @ロール`')
                return
            
            target_type = target_type.lower()
            
            if target_type == "user":
                # ユーザーを削除
                user = None
                if ctx.message.mentions:
                    user = ctx.message.mentions[0]
                else:
                    # IDで検索
                    try:
                        user_id = int(target.strip('<@!>'))
                        user = ctx.guild.get_member(user_id)
                    except ValueError:
                        await ctx.send('❌ 有効なユーザーを指定してください。')
                        return
                
                if not user:
                    await ctx.send('❌ ユーザーが見つかりませんでした。')
                    return
                
                if user.id not in whitelist['users']:
                    await ctx.send(f'❌ {user.mention} はホワイトリストに登録されていません。')
                    return
                
                whitelist['users'].remove(user.id)
                embed = discord.Embed(
                    title="✅ ユーザー削除完了",
                    description=f"{user.mention} をホワイトリストから削除しました。",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            elif target_type == "role":
                # ロールを削除
                role = None
                if ctx.message.role_mentions:
                    role = ctx.message.role_mentions[0]
                else:
                    # 名前またはIDで検索
                    role = discord.utils.get(ctx.guild.roles, name=target) or discord.utils.get(ctx.guild.roles, id=int(target) if target.isdigit() else None)
                
                if not role:
                    await ctx.send('❌ ロールが見つかりませんでした。')
                    return
                
                if role.id not in whitelist['roles']:
                    await ctx.send(f'❌ {role.mention} はホワイトリストに登録されていません。')
                    return
                
                whitelist['roles'].remove(role.id)
                embed = discord.Embed(
                    title="✅ ロール削除完了",
                    description=f"{role.mention} をホワイトリストから削除しました。",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            else:
                await ctx.send('❌ 無効な対象タイプです。`user` または `role` を指定してください。')
                
        elif action == "list":
            # ホワイトリストの内容を表示
            embed = discord.Embed(
                title="📋 ホワイトリスト一覧",
                color=discord.Color.blue()
            )
            
            # 登録ユーザーを表示
            if whitelist['users']:
                user_list = []
                for user_id in list(whitelist['users'])[:10]:  # 最大10人まで表示
                    user = ctx.guild.get_member(user_id)
                    if user:
                        user_list.append(user.mention)
                    else:
                        user_list.append(f"<@{user_id}> (未発見)")
                
                user_text = "\n".join(user_list)
                if len(whitelist['users']) > 10:
                    user_text += f"\n... 他 {len(whitelist['users']) - 10} 人"
                
                embed.add_field(
                    name=f"👤 登録ユーザー ({len(whitelist['users'])}人)",
                    value=user_text,
                    inline=False
                )
            else:
                embed.add_field(name="👤 登録ユーザー", value="なし", inline=False)
            
            # 登録ロールを表示
            if whitelist['roles']:
                role_list = []
                for role_id in list(whitelist['roles'])[:10]:  # 最大10個まで表示
                    role = ctx.guild.get_role(role_id)
                    if role:
                        role_list.append(role.mention)
                    else:
                        role_list.append(f"削除されたロール (ID: {role_id})")
                
                role_text = "\n".join(role_list)
                if len(whitelist['roles']) > 10:
                    role_text += f"\n... 他 {len(whitelist['roles']) - 10} 個"
                
                embed.add_field(
                    name=f"🏷️ 登録ロール ({len(whitelist['roles'])}個)",
                    value=role_text,
                    inline=False
                )
            else:
                embed.add_field(name="🏷️ 登録ロール", value="なし", inline=False)
            
            status = "🟢 有効" if whitelist['enabled'] else "🔴 無効"
            embed.add_field(name="状態", value=status, inline=True)
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action == "clear":
            # ホワイトリストをクリア
            if not whitelist['users'] and not whitelist['roles']:
                await ctx.send('❌ ホワイトリストは既に空です。')
                return
            
            # 確認メッセージ
            total_entries = len(whitelist['users']) + len(whitelist['roles'])
            await ctx.send(f'🗑️ ホワイトリストをクリアしますか？\n'
                          f'登録されている {total_entries} 件のエントリがすべて削除されます。\n'
                          f'続行する場合は `yes` と入力してください（30秒以内）')
            
            def check(message):
                return (message.author == ctx.author and 
                       message.channel == ctx.channel and 
                       message.content.lower() == 'yes')
            
            try:
                confirmation = await bot.wait_for('message', check=check, timeout=30.0)
                whitelist['users'].clear()
                whitelist['roles'].clear()
                
                embed = discord.Embed(
                    title="✅ ホワイトリストクリア完了",
                    description=f"ホワイトリストをクリアしました。（{total_entries} 件のエントリを削除）",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            except asyncio.TimeoutError:
                await ctx.send('⏰ 確認がタイムアウトしました。クリアがキャンセルされました')
                
        else:
            # 無効なアクション
            await ctx.send(f'❌ 無効なアクションです: `{action}`\n'
                          f'使用可能: status, enable, disable, add, remove, list, clear')
            
    except Exception as e:
        await ctx.send(f'❌ ホワイトリストコマンドの実行中にエラーが発生しました: {e}')
        print(f"ホワイトリストコマンドエラー: {type(e).__name__}: {e}")

@whitelist.error
async def whitelist_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ このコマンドはサーバー管理権限を持つユーザーのみ使用できます')

@bot.command(name='banword')
@commands.has_permissions(manage_messages=True)
async def banword(ctx, action: str = "status", *, target: Optional[str] = None):
    """
    禁止ワード管理コマンド
    使用例:
    n!banword status - 禁止ワードの状態を表示
    n!banword enable - 禁止ワードを有効にする
    n!banword disable - 禁止ワードを無効にする
    n!banword add 単語 - 禁止ワードを追加
    n!banword remove 単語 - 禁止ワードを削除
    n!banword list - 禁止ワードリストを表示
    n!banword clear - 禁止ワードをクリア
    n!banword settings - 詳細設定を表示
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    guild_id = ctx.guild.id
    banword_settings = banword_data[guild_id]
    action = action.lower()
    
    try:
        if action == "status":
            # 禁止ワードの現在の状態を表示
            embed = discord.Embed(
                title="🚫 禁止ワードステータス",
                color=discord.Color.green() if banword_settings['enabled'] else discord.Color.red()
            )
            
            status = "🟢 有効" if banword_settings['enabled'] else "🔴 無効"
            embed.add_field(name="現在の状態", value=status, inline=True)
            
            embed.add_field(name="禁止ワード数", value=f"{len(banword_settings['words'])}個", inline=True)
            
            action_text = {
                'delete': '🗑️ 削除',
                'warn': '⚠️ 警告',
                'mute': '🔇 ミュート'
            }.get(banword_settings['action'], banword_settings['action'])
            embed.add_field(name="対処方法", value=action_text, inline=True)
            
            case_text = "有効" if banword_settings['case_sensitive'] else "無効"
            embed.add_field(name="大文字小文字区別", value=case_text, inline=True)
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action == "enable":
            # 禁止ワードを有効にする
            banword_settings['enabled'] = True
            embed = discord.Embed(
                title="✅ 禁止ワード有効化",
                description="禁止ワード機能を有効にしました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        elif action == "disable":
            # 禁止ワードを無効にする
            banword_settings['enabled'] = False
            embed = discord.Embed(
                title="🔴 禁止ワード無効化",
                description="禁止ワード機能を無効にしました。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            
        elif action == "add":
            if not target:
                await ctx.send('❌ 追加する禁止ワードを指定してください。\n使用例: `n!banword add 不適切な言葉`')
                return
            
            word = target.strip()
            if not word:
                await ctx.send('❌ 有効な禁止ワードを指定してください。')
                return
            
            if len(word) > 100:
                await ctx.send('❌ 禁止ワードは100文字以下にしてください。')
                return
            
            # 既に登録されているかチェック
            check_word = word if banword_settings['case_sensitive'] else word.lower()
            existing_words = [w if banword_settings['case_sensitive'] else w.lower() for w in banword_settings['words']]
            
            if check_word in existing_words:
                await ctx.send(f'❌ 「{word}」は既に禁止ワードに登録されています。')
                return
            
            banword_settings['words'].add(word)
            embed = discord.Embed(
                title="✅ 禁止ワード追加完了",
                description=f"「{word}」を禁止ワードに追加しました。",
                color=discord.Color.green()
            )
            embed.add_field(name="現在の禁止ワード数", value=f"{len(banword_settings['words'])}個", inline=True)
            await ctx.send(embed=embed)
            
        elif action == "remove":
            if not target:
                await ctx.send('❌ 削除する禁止ワードを指定してください。\n使用例: `n!banword remove 単語`')
                return
            
            word = target.strip()
            if not word:
                await ctx.send('❌ 有効な禁止ワードを指定してください。')
                return
            
            # 大文字小文字を考慮して検索
            word_to_remove = None
            for existing_word in banword_settings['words']:
                if banword_settings['case_sensitive']:
                    if existing_word == word:
                        word_to_remove = existing_word
                        break
                else:
                    if existing_word.lower() == word.lower():
                        word_to_remove = existing_word
                        break
            
            if not word_to_remove:
                await ctx.send(f'❌ 「{word}」は禁止ワードに登録されていません。')
                return
            
            banword_settings['words'].remove(word_to_remove)
            embed = discord.Embed(
                title="✅ 禁止ワード削除完了",
                description=f"「{word_to_remove}」を禁止ワードから削除しました。",
                color=discord.Color.green()
            )
            embed.add_field(name="現在の禁止ワード数", value=f"{len(banword_settings['words'])}個", inline=True)
            await ctx.send(embed=embed)
            
        elif action == "list":
            # 禁止ワードリストを表示
            embed = discord.Embed(
                title="🚫 禁止ワード一覧",
                color=discord.Color.blue()
            )
            
            if banword_settings['words']:
                word_list = list(banword_settings['words'])
                word_list.sort()
                
                # 最大20個まで表示
                display_words = word_list[:20]
                word_text = "\n".join([f"• {word}" for word in display_words])
                
                if len(word_list) > 20:
                    word_text += f"\n... 他 {len(word_list) - 20} 個"
                
                embed.add_field(
                    name=f"📝 禁止ワード ({len(word_list)}個)",
                    value=word_text,
                    inline=False
                )
            else:
                embed.add_field(name="📝 禁止ワード", value="なし", inline=False)
            
            status = "🟢 有効" if banword_settings['enabled'] else "🔴 無効"
            embed.add_field(name="状態", value=status, inline=True)
            
            action_text = {
                'delete': '🗑️ 削除',
                'warn': '⚠️ 警告',
                'mute': '🔇 ミュート'
            }.get(banword_settings['action'], banword_settings['action'])
            embed.add_field(name="対処方法", value=action_text, inline=True)
            
            case_text = "有効" if banword_settings['case_sensitive'] else "無効"
            embed.add_field(name="大文字小文字区別", value=case_text, inline=True)
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action == "clear":
            # 禁止ワードをクリア
            if not banword_settings['words']:
                await ctx.send('❌ 禁止ワードリストは既に空です。')
                return
            
            # 確認メッセージ
            word_count = len(banword_settings['words'])
            await ctx.send(f'🗑️ 禁止ワードをすべてクリアしますか？\n'
                          f'登録されている {word_count} 個の禁止ワードがすべて削除されます。\n'
                          f'続行する場合は `yes` と入力してください（30秒以内）')
            
            def check(message):
                return (message.author == ctx.author and 
                       message.channel == ctx.channel and 
                       message.content.lower() == 'yes')
            
            try:
                confirmation = await bot.wait_for('message', check=check, timeout=30.0)
                banword_settings['words'].clear()
                
                embed = discord.Embed(
                    title="✅ 禁止ワードクリア完了",
                    description=f"禁止ワードをクリアしました。（{word_count} 個の禁止ワードを削除）",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                
            except asyncio.TimeoutError:
                await ctx.send('⏰ 確認がタイムアウトしました。クリアがキャンセルされました')
                
        elif action == "settings":
            # 詳細設定を表示
            embed = discord.Embed(
                title="⚙️ 禁止ワード詳細設定",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="機能設定",
                value=f"""
🟢 **有効状態**: {"有効" if banword_settings['enabled'] else "無効"}
🔤 **大文字小文字区別**: {"有効" if banword_settings['case_sensitive'] else "無効"}
🎯 **対処方法**: {banword_settings['action']}
📝 **登録ワード数**: {len(banword_settings['words'])}個
                """,
                inline=False
            )
            
            embed.add_field(
                name="対処方法の説明",
                value="""
🗑️ **delete**: 禁止ワードを含むメッセージを削除
⚠️ **warn**: 警告メッセージを送信（メッセージは残す）
🔇 **mute**: メッセージ削除 + ユーザーを30分ミュート
                """,
                inline=False
            )
            
            embed.add_field(
                name="設定変更方法",
                value="""
対処方法変更: `n!banword setaction delete/warn/mute`
大文字小文字区別: `n!banword setcase on/off`
                """,
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        elif action == "setaction":
            # 対処方法を設定
            if not target or target.lower() not in ['delete', 'warn', 'mute']:
                await ctx.send('❌ 有効な対処方法を指定してください。\n使用例: `n!banword setaction delete` (delete/warn/mute)')
                return
            
            new_action = target.lower()
            old_action = banword_settings['action']
            banword_settings['action'] = new_action
            
            action_text = {
                'delete': '🗑️ 削除',
                'warn': '⚠️ 警告',
                'mute': '🔇 ミュート'
            }
            
            embed = discord.Embed(
                title="✅ 対処方法変更完了",
                description=f"対処方法を「{action_text[old_action]}」から「{action_text[new_action]}」に変更しました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        elif action == "setcase":
            # 大文字小文字区別を設定
            if not target or target.lower() not in ['on', 'off', 'true', 'false', '有効', '無効']:
                await ctx.send('❌ 有効な設定を指定してください。\n使用例: `n!banword setcase on` (on/off)')
                return
            
            new_case = target.lower() in ['on', 'true', '有効']
            old_case = banword_settings['case_sensitive']
            banword_settings['case_sensitive'] = new_case
            
            embed = discord.Embed(
                title="✅ 大文字小文字区別設定変更完了",
                description=f"大文字小文字区別を「{'有効' if old_case else '無効'}」から「{'有効' if new_case else '無効'}」に変更しました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        else:
            # 無効なアクション
            await ctx.send(f'❌ 無効なアクションです: `{action}`\n'
                          f'使用可能: status, enable, disable, add, remove, list, clear, settings, setaction, setcase')
            
    except Exception as e:
        await ctx.send(f'❌ 禁止ワードコマンドの実行中にエラーが発生しました: {e}')
        print(f"禁止ワードコマンドエラー: {type(e).__name__}: {e}")

@banword.error
async def banword_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ このコマンドはメッセージ管理権限を持つユーザーのみ使用できます')

@bot.command(name='helpbot')
async def help_command(ctx):
    """ボットの使い方を表示するヘルプコマンド"""
    embed = discord.Embed(
        title="🤖 Discord Bot ヘルプ",
        description="このボットの使用方法とコマンド一覧です",
        color=discord.Color.blue()
         )
    # 管理コマンド
    embed.add_field(
        name="🛡️ 管理コマンド",
        value="""
`n!ban @ユーザー 理由` - ユーザーをバン
`n!ban 123456789 理由` - IDでバン（サーバー外も可）
`n!unban 123456789 理由` - バンを解除
`n!role_status` - ボットのロール状態を確認
`n!cleanup_role` - 管理者専用ロール削除
`n!antispam` - スパム対策の設定・管理
`n!whitelist` - ホワイトリスト管理（詳細は後述）
`n!banword` - 禁止ワード管理（詳細は後述）
        """,
        inline=False
    )
    
    # 情報コマンド
    embed.add_field(
        name="📊 情報コマンド",
        value="""
`n!serverinfo` - サーバーの詳細情報を表示
`n!auditlog` - サーバーの監査ログを表示
`n!userinfo` - ユーザー情報を表示（自分または指定ユーザー）
        """,
        inline=False
    )
    
    # エンターテイメント
    embed.add_field(
        name="🎮 エンターテイメント",
        value="""
`n!supurito` - Sprite画像をランダムに表示
`n!dice` - 6面ダイスを1個振る
`n!dice 20` - 20面ダイスを1個振る  
`n!dice 3d6` - 6面ダイスを3個振る
`n!dice 2d20` - 20面ダイスを2個振る
**制限:** 1-20個、2-1000面
`n!fizzbuzz` - FizzBuzzゲーム（ルール表示）
`n!fizzbuzz 15` - 数字を入力してFizzBuzzの結果を表示     
        """,
        inline=False
    )
    
    # ホワイトリスト詳細コマンド
    embed.add_field(
        name="📋 ホワイトリストコマンド",
        value="""
`n!whitelist status` - ホワイトリストの現在の状態を表示
`n!whitelist enable/disable` - ホワイトリストを有効/無効に
`n!whitelist add user @ユーザー` - ユーザーを追加
`n!whitelist remove user @ユーザー` - ユーザーを削除
`n!whitelist add role @ロール` - ロールを追加
`n!whitelist remove role @ロール` - ロールを削除
`n!whitelist list` - 登録されたユーザーとロールを表示
`n!whitelist clear` - ホワイトリストをクリア
**権限:** サーバー管理権限が必要
        """,
        inline=False
    )
    
    # 禁止ワード詳細コマンド
    embed.add_field(
        name="🚫 禁止ワードコマンド",
        value="""
`n!banword status` - 禁止ワードの現在の状態を表示
`n!banword enable/disable` - 禁止ワードを有効/無効に
`n!banword add 単語` - 禁止ワードを追加
`n!banword remove 単語` - 禁止ワードを削除
`n!banword list` - 禁止ワードリストを表示
`n!banword clear` - 禁止ワードをクリア
`n!banword settings` - 詳細設定を表示
**権限:** メッセージ管理権限が必要
**機能:** 禁止ワードを含むメッセージを自動削除
        """,
        inline=False
    )
    
    embed.set_footer(
        text=f"要求者: {ctx.author.display_name} | すべてのコマンドは日本語で応答します",
        icon_url=ctx.author.display_avatar.url
    )
    
    await ctx.send(embed=embed)

@bot.command(name='role_status')
async def role_status(ctx):
    """現在のロール状態を確認"""
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    try:
        bot_member = ctx.guild.me
        if not bot_member:
            await ctx.send('❌ ボット情報を取得できませんでした')
            return
        
        target_role = discord.utils.get(ctx.guild.roles, name=ROLE_NAME)
        
        # 詳細な状態情報を提供
        embed = discord.Embed(title=f"ロール状態: {ROLE_NAME}", color=discord.Color.blue())
        
        if target_role:
            has_role = target_role in bot_member.roles
            can_manage = (bot_member.guild_permissions.manage_roles and 
                         target_role < bot_member.top_role)
            
            embed.add_field(name="ロール存在", value="✅ あり", inline=True)
            embed.add_field(name="ボットに付与", value="✅ 済み" if has_role else "❌ なし", inline=True)
            embed.add_field(name="管理可能", value="✅ 可能" if can_manage else "❌ 不可", inline=True)
            
            if not can_manage and bot_member.guild_permissions.manage_roles:
                embed.add_field(name="注意", 
                               value=f"ロールがボットより上位にあります。管理者にボットのロールを上に移動してもらってください。", 
                               inline=False)
            elif not bot_member.guild_permissions.manage_roles:
                embed.add_field(name="注意", 
                               value="ボットにロール管理権限がありません。", 
                               inline=False)
        else:
            embed.add_field(name="ロール存在", value="❌ なし", inline=True)
            embed.add_field(name="ボットに付与", value="❌ なし", inline=True)
            embed.add_field(name="管理可能", value="❌ ロールなし", inline=True)
            
        await ctx.send(embed=embed)
            
    except Exception as e:
        await ctx.send(f'❌ ロール状態確認中にエラーが発生しました: {e}')
@bot.command(name='cleanup_role')
@commands.has_permissions(administrator=True)
async def cleanup_role(ctx):
    """管理者専用: Level Cannies ηロールを削除"""
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    try:
        target_role = discord.utils.get(ctx.guild.roles, name=ROLE_NAME)
        
        if not target_role:
            await ctx.send(f'❌ ロール「{ROLE_NAME}」が見つかりません')
            return
        
        if target_role.managed:
            await ctx.send(f'❌ ロール「{ROLE_NAME}」は管理されたロールのため削除できません')
            return
        
        # 確認メッセージ
        await ctx.send(f'🗑️ ロール「{ROLE_NAME}」を削除しますか？\n'
                      f'このロールを持つ全メンバー（{len(target_role.members)}人）から削除されます。\n'
                      f'続行する場合は `yes` と入力してください（30秒以内）')
        
        def check(message):
            return (message.author == ctx.author and 
                   message.channel == ctx.channel and 
                   message.content.lower() == 'yes')
        
        try:
            confirmation = await bot.wait_for('message', check=check, timeout=30.0)
            await target_role.delete(reason=f"管理者 {ctx.author} による手動削除")
            await ctx.send(f'✅ ロール「{ROLE_NAME}」を削除しました')
            
        except asyncio.TimeoutError:
            await ctx.send('⏰ 確認がタイムアウトしました。削除がキャンセルされました')
            
    except discord.Forbidden:
        await ctx.send('❌ ロール削除権限がありません')
    except Exception as e:
        await ctx.send(f'❌ ロール削除中にエラーが発生しました: {e}')

@cleanup_role.error
async def cleanup_role_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ このコマンドは管理者のみ使用できます')

@bot.command(name='ban')
@commands.has_permissions(ban_members=True)
async def ban_user(ctx, target, *, reason="理由が指定されていません"):
    """
    ユーザーをバンするコマンド
    使用例:
    n!ban @ユーザー 荒らし行為のため
    n!ban 123456789012345678 スパム行為のため
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    # ボットの権限をチェック
    if not ctx.guild.me.guild_permissions.ban_members:
        await ctx.send('❌ ボットにメンバーをバンする権限がありません')
        return
    
    try:
        # ターゲットを特定
        user_to_ban = None
        
        # メンション形式の場合
        if ctx.message.mentions:
            user_to_ban = ctx.message.mentions[0]
        
        # ユーザーIDの場合（数字のみ）
        elif target.isdigit():
            user_id = int(target)
            try:
                # まずサーバー内のメンバーを検索
                user_to_ban = ctx.guild.get_member(user_id)
                if not user_to_ban:
                    # サーバーにいない場合はDiscord APIから取得を試行
                    try:
                        user_to_ban = await bot.fetch_user(user_id)
                    except (discord.NotFound, discord.HTTPException):
                        # fetch_userが失敗してもObjectとしてバン可能
                        # Objectクラスは直接バン可能だが、表示用に情報を保持する必要がある
                        user_to_ban = discord.Object(id=user_id)
                        # 表示用の情報を設定（カスタム属性として保存）
                        setattr(user_to_ban, '_display_name', f"Unknown User ({user_id})")
                        setattr(user_to_ban, '_is_unknown', True)
            except discord.NotFound:
                await ctx.send(f'❌ ID `{user_id}` のユーザーが見つかりません')
                return
            except discord.HTTPException:
                await ctx.send(f'❌ ユーザー情報の取得中にエラーが発生しました')
                return
        
        # ユーザー名#判別子の場合
        elif '#' in target:
            try:
                username, discriminator = target.rsplit('#', 1)
                for member in ctx.guild.members:
                    if member.name == username and member.discriminator == discriminator:
                        user_to_ban = member
                        break
                if not user_to_ban:
                    await ctx.send(f'❌ ユーザー `{target}` がサーバー内に見つかりません')
                    return
            except ValueError:
                await ctx.send('❌ 無効なユーザー形式です')
                return
        
        else:
            await ctx.send('❌ 無効なユーザー指定です。メンション、ユーザーID、またはユーザー名#番号を使用してください')
            return
        
        if not user_to_ban:
            await ctx.send('❌ ユーザーを特定できませんでした')
            return
        
        # 自分自身やボットをバンしようとした場合
        if user_to_ban.id == ctx.author.id:
            await ctx.send('❌ 自分自身をバンすることはできません')
            return
        
        if bot.user and user_to_ban.id == bot.user.id:
            await ctx.send('❌ ボット自身をバンすることはできません')
            return
        
        # サーバーの所有者をバンしようとした場合
        if user_to_ban.id == ctx.guild.owner_id:
            await ctx.send('❌ サーバーの所有者をバンすることはできません')
            return
        
        # メンバーの場合、権限階層をチェック
        if isinstance(user_to_ban, discord.Member):
            if user_to_ban.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
                await ctx.send('❌ あなたより上位または同等の権限を持つユーザーをバンすることはできません')
                return
            
            if user_to_ban.top_role >= ctx.guild.me.top_role:
                await ctx.send('❌ ボットより上位または同等の権限を持つユーザーをバンすることはできません')
                return
        
        # 既にバンされているかチェック
        try:
            ban_entry = await ctx.guild.fetch_ban(user_to_ban)
            await ctx.send(f'❌ {user_to_ban} は既にバンされています\n理由: {ban_entry.reason or "理由なし"}')
            return
        except discord.NotFound:
            # バンされていない場合は正常
            pass
        except discord.Forbidden:
            # 権限がない場合はスキップ
            pass
        
        # 確認メッセージ
        # 表示名を決定
        is_unknown = hasattr(user_to_ban, '_is_unknown') and getattr(user_to_ban, '_is_unknown', False)
        display_name = ""  # 初期化
        if is_unknown:
            display_name = getattr(user_to_ban, '_display_name', f"Unknown User ({user_to_ban.id})")
            user_mention = f"`{display_name}`"
        elif isinstance(user_to_ban, discord.Member):
            user_mention = user_to_ban.mention
        else:
            user_mention = f"`{user_to_ban}`"
            
        embed = discord.Embed(
            title="🔨 ユーザーバン確認",
            description=f"本当に {user_mention} をバンしますか？",
            color=discord.Color.red()
        )
        
        # 対象ユーザー情報の表示
        if is_unknown:
            embed.add_field(name="対象ユーザー", value=f"{display_name} (ID: {user_to_ban.id})", inline=False)
        else:
            embed.add_field(name="対象ユーザー", value=f"{user_to_ban} (ID: {user_to_ban.id})", inline=False)
        embed.add_field(name="理由", value=reason, inline=False)
        embed.add_field(name="実行者", value=ctx.author.mention, inline=True)
        
        if isinstance(user_to_ban, discord.Member):
            embed.add_field(name="サーバー参加日", 
                           value=user_to_ban.joined_at.strftime("%Y/%m/%d %H:%M") if user_to_ban.joined_at else "不明", 
                           inline=True)
        
        embed.set_footer(text="続行する場合は 'yes' と入力してください（30秒以内）")
        
        await ctx.send(embed=embed)
        
        # 確認待ち
        def check(message):
            return (message.author == ctx.author and 
                   message.channel == ctx.channel and 
                   message.content.lower() == 'yes')
        
        try:
            confirmation = await bot.wait_for('message', check=check, timeout=30.0)
            
            # 理由の長さ制限（Discord API制限対応）
            full_reason = f"実行者: {ctx.author} | 理由: {reason}"
            if len(full_reason) > 512:
                full_reason = full_reason[:509] + "..."
            
            # バンの実行
            await ctx.guild.ban(
                user_to_ban, 
                reason=full_reason,
                delete_message_seconds=0  # discord.py v2対応
            )
            
            # 成功メッセージ
            success_embed = discord.Embed(
                title="✅ バン実行完了",
                description=f"{user_to_ban} をバンしました",
                color=discord.Color.green()
            )
            success_embed.add_field(name="理由", value=reason, inline=False)
            success_embed.add_field(name="実行者", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=success_embed)
            
            # ログ出力
            print(f"🔨 バン実行: {user_to_ban} (ID: {user_to_ban.id}) | 理由: {reason} | 実行者: {ctx.author}")
            
        except asyncio.TimeoutError:
            await ctx.send('⏰ 確認がタイムアウトしました。バンがキャンセルされました')
            
    except discord.Forbidden:
        await ctx.send('❌ バン権限が不足しています')
    except discord.HTTPException as e:
        await ctx.send(f'❌ バン実行中にエラーが発生しました: {e}')
    except Exception as e:
        await ctx.send(f'❌ 予期しないエラーが発生しました: {e}')
        print(f"バンコマンドエラー: {type(e).__name__}: {e}")

@ban_user.error
async def ban_user_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ このコマンドはメンバーをバンする権限を持つユーザーのみ使用できます')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('❌ バン対象のユーザーを指定してください\n'
                      '使用例: `!ban @ユーザー 理由` または `!ban 123456789 理由`')

@bot.command(name='unban')
@commands.has_permissions(ban_members=True)
async def unban_user(ctx, user_id: int, *, reason="理由が指定されていません"):
    """
    ユーザーのバンを解除するコマンド
    使用例:
    n!unban 123456789012345678 誤バンのため
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    # ボットの権限をチェック
    if not ctx.guild.me.guild_permissions.ban_members:
        await ctx.send('❌ ボットにメンバーをバンする権限がありません')
        return
    
    try:
        # ユーザーがバンされているかチェック
        ban_entry = await ctx.guild.fetch_ban(discord.Object(id=user_id))
        banned_user = ban_entry.user
        
        # 確認メッセージ
        embed = discord.Embed(
            title="🔓 バン解除確認",
            description=f"本当に {banned_user} のバンを解除しますか？",
            color=discord.Color.green()
        )
        embed.add_field(name="対象ユーザー", value=f"{banned_user} (ID: {banned_user.id})", inline=False)
        embed.add_field(name="現在のバン理由", value=ban_entry.reason or "理由なし", inline=False)
        embed.add_field(name="解除理由", value=reason, inline=False)
        embed.add_field(name="実行者", value=ctx.author.mention, inline=True)
        embed.set_footer(text="続行する場合は 'yes' と入力してください（30秒以内）")
        
        await ctx.send(embed=embed)
        
        # 確認待ち
        def check(message):
            return (message.author == ctx.author and 
                   message.channel == ctx.channel and 
                   message.content.lower() == 'yes')
        
        try:
            confirmation = await bot.wait_for('message', check=check, timeout=30.0)
            
            # バン解除の実行
            await ctx.guild.unban(banned_user, reason=f"実行者: {ctx.author} | 理由: {reason}")
            
            # 成功メッセージ
            success_embed = discord.Embed(
                title="✅ バン解除完了",
                description=f"{banned_user} のバンを解除しました",
                color=discord.Color.green()
            )
            success_embed.add_field(name="解除理由", value=reason, inline=False)
            success_embed.add_field(name="実行者", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=success_embed)
            
            # ログ出力
            print(f"🔓 バン解除: {banned_user} (ID: {banned_user.id}) | 理由: {reason} | 実行者: {ctx.author}")
            
        except asyncio.TimeoutError:
            await ctx.send('⏰ 確認がタイムアウトしました。バン解除がキャンセルされました')
        
    except discord.NotFound:
        await ctx.send(f'❌ ID `{user_id}` のユーザーはバンされていません')
    except discord.Forbidden:
        await ctx.send('❌ バン解除権限が不足しています')
    except discord.HTTPException as e:
        await ctx.send(f'❌ バン解除中にエラーが発生しました: {e}')
    except Exception as e:
        await ctx.send(f'❌ 予期しないエラーが発生しました: {e}')
        print(f"アンバンコマンドエラー: {type(e).__name__}: {e}")

@unban_user.error
async def unban_user_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ このコマンドはメンバーをバンする権限を持つユーザーのみ使用できます')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('❌ バン解除するユーザーのIDを指定してください\n'
                      '使用例: `!unban 123456789 解除理由`')
    elif isinstance(error, commands.BadArgument):
        await ctx.send('❌ 有効なユーザーIDを指定してください（数字のみ）')

@bot.command(name='serverinfo')
async def server_info(ctx):
    """
    サーバーの詳細情報を表示するコマンド
    使用例: n!serverinfo
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます')
        return
    
    try:
        guild = ctx.guild
        
        # 基本情報の取得
        owner = guild.owner
        created_at = guild.created_at
        member_count = guild.member_count
        
        # チャンネル数の計算
        text_channels = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
        voice_channels = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
        categories = len([c for c in guild.channels if isinstance(c, discord.CategoryChannel)])
        total_channels = len(guild.channels)
        
        # ロール数
        role_count = len(guild.roles) - 1  # @everyone ロールを除く
        
        # メンバー統計
        humans = len([m for m in guild.members if not m.bot])
        bots = len([m for m in guild.members if m.bot])
        
        # オンライン状況（オンライン、アイドル、取り込み中、オフライン）
        online_members = len([m for m in guild.members if m.status == discord.Status.online])
        idle_members = len([m for m in guild.members if m.status == discord.Status.idle])
        dnd_members = len([m for m in guild.members if m.status == discord.Status.dnd])
        offline_members = len([m for m in guild.members if m.status == discord.Status.offline])
        
        # サーバーレベルと機能
        verification_level = str(guild.verification_level).replace('_', ' ').title()
        content_filter = str(guild.explicit_content_filter).replace('_', ' ').title()
        
        # サーバーブースト情報
        boost_level = guild.premium_tier
        boost_count = guild.premium_subscription_count or 0
        
        # サーバー機能
        features = []
        if guild.features:
            feature_names = {
                'VERIFIED': '✅ 認証済み',
                'PARTNERED': '🤝 パートナー',
                'COMMUNITY': '🏘️ コミュニティ',
                'NEWS': '📰 ニュース',
                'DISCOVERABLE': '🔍 発見可能',
                'VANITY_URL': '🔗 カスタムURL',
                'BANNER': '🎨 バナー',
                'ANIMATED_ICON': '✨ アニメーションアイコン',
                'BOOST_LEVEL_1': '🚀 ブーストレベル1',
                'BOOST_LEVEL_2': '🚀 ブーストレベル2',
                'BOOST_LEVEL_3': '🚀 ブーストレベル3'
            }
            features = [feature_names.get(f, f) for f in guild.features[:10]]  # 最大10個まで
        
        # Embedの作成
        embed = discord.Embed(
            title=f"📊 {guild.name} サーバー情報",
            description=f"サーバーID: `{guild.id}`",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        
        # サーバーアイコンの設定
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        # 基本情報
        embed.add_field(
            name="👑 所有者",
            value=f"{owner.mention if owner else '不明'}\n`{owner}` (ID: {owner.id})" if owner else "不明",
            inline=True
        )
        
        embed.add_field(
            name="📅 作成日",
            value=f"{created_at.strftime('%Y年%m月%d日')}\n({(discord.utils.utcnow() - created_at).days}日前)",
            inline=True
        )
        
        embed.add_field(
            name="🔒 認証レベル",
            value=verification_level,
            inline=True
        )
        
        # メンバー情報
        embed.add_field(
            name="👥 メンバー数",
            value=f"**総数**: {member_count:,}\n"
                  f"👤 人間: {humans:,}\n"
                  f"🤖 ボット: {bots:,}",
            inline=True
        )
        
        # オンライン状況
        embed.add_field(
            name="📈 オンライン状況",
            value=f"🟢 オンライン: {online_members}\n"
                  f"🟡 退席中: {idle_members}\n"
                  f"🔴 取り込み中: {dnd_members}\n"
                  f"⚫ オフライン: {offline_members}",
            inline=True
        )
        
        # チャンネル情報
        embed.add_field(
            name="📺 チャンネル",
            value=f"**総数**: {total_channels}\n"
                  f"💬 テキスト: {text_channels}\n"
                  f"🔊 ボイス: {voice_channels}\n"
                  f"📁 カテゴリ: {categories}",
            inline=True
        )
        
        # ロール情報
        embed.add_field(
            name="🎭 ロール数",
            value=f"{role_count:,}",
            inline=True
        )
        
        # ブースト情報
        if boost_level > 0 or boost_count > 0:
            boost_emoji = ["", "🥉", "🥈", "🥇"][boost_level] if boost_level < 4 else "💎"
            embed.add_field(
                name=f"{boost_emoji} サーバーブースト",
                value=f"レベル {boost_level}\n{boost_count} ブースト",
                inline=True
            )
        else:
            embed.add_field(
                name="🚀 サーバーブースト",
                value="未ブースト",
                inline=True
        )
        
        # コンテンツフィルタ
        embed.add_field(
            name="🛡️ コンテンツフィルタ",
            value=content_filter,
            inline=True
        )
        
        # サーバー機能
        if features:
            embed.add_field(
                name="⭐ サーバー機能",
                value="\n".join(features),
                inline=False
        )
        
        # フッター
        embed.set_footer(
            text=f"情報取得者: {ctx.author}",
            icon_url=ctx.author.display_avatar.url
        )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f'❌ サーバー情報の取得中にエラーが発生しました: {e}')
        print(f"サーバー情報コマンドエラー: {type(e).__name__}: {e}")

@bot.command(name='supurito')
async def supurito(ctx):
    """
    Spriteの画像をランダムに1枚送信するコマンド
    使用例: n!supurito
    """
    try:
        # 画像ディレクトリのパス
        sprite_dir = "sprite_images"
        
        # 指定された4枚のSprite画像ファイル名
        specific_images = [
            "sprite_bottle_1.png",
            "sprite_can_1.png", 
            "sprite_bottle_2.jpg",
            "sprite_image_3.jpeg"
        ]
        
        # 指定された画像ファイルの完全パスを作成
        image_files = [os.path.join(sprite_dir, img) for img in specific_images]
        
        # 存在する画像ファイルのみをフィルタリング
        image_files = [img for img in image_files if os.path.exists(img)]
        
        # 画像ファイルが存在するかチェック
        if not image_files:
            await ctx.send('❌ Sprite画像が見つかりません。管理者に連絡してください。')
            print(f"Sprite画像が見つかりません。ディレクトリ: {sprite_dir}")
            return
        
        # ランダムに1枚選択
        selected_image = random.choice(image_files)
        
        # ファイルが存在するかチェック
        if not os.path.exists(selected_image):
            await ctx.send('❌ 選択された画像ファイルが見つかりません。')
            print(f"画像ファイルが見つかりません: {selected_image}")
            return
        
        # ファイルサイズをチェック（Discord制限: 8MB）
        file_size = os.path.getsize(selected_image)
        if file_size > 8 * 1024 * 1024:  # 8MB
            await ctx.send('❌ 選択された画像ファイルが大きすぎます（8MB制限）')
            print(f"ファイルサイズが大きすぎます: {selected_image} ({file_size} bytes)")
            return
        
        # ファイル名を取得（表示用）
        filename = os.path.basename(selected_image)
        
        # 画像を送信
        with open(selected_image, 'rb') as f:
            picture = discord.File(f, filename=filename)
            
            # Embedメッセージを作成
            embed = discord.Embed(
                title="🥤 Sprite Random!",
                description=f"ランダムに選ばれたSprite画像です！",
                color=discord.Color.green()
            )
            embed.set_footer(
                text=f"画像: {filename} | 要求者: {ctx.author.display_name}",
                icon_url=ctx.author.display_avatar.url
            )
            
            await ctx.send(file=picture, embed=embed)
            
        # ログ出力
        print(f"🥤 Sprite画像送信: {filename} | 要求者: {ctx.author}")
        
    except discord.HTTPException as e:
        await ctx.send(f'❌ 画像の送信中にエラーが発生しました: {e}')
        print(f"Discord HTTPエラー: {e}")
    except Exception as e:
        await ctx.send(f'❌ 予期しないエラーが発生しました: {e}')
        print(f"Supuraitoコマンドエラー: {type(e).__name__}: {e}")

@bot.command(name='auditlog')
async def auditlog(ctx, limit: int = 10):
    """
    サーバーの監査ログを表示するコマンド
    使用例: n!auditlog
    使用例: n!auditlog 20
    """
    # 権限チェック
    if not ctx.author.guild_permissions.view_audit_log:
        await ctx.send('❌ 監査ログを表示する権限がありません。サーバー管理者に連絡してください。')
        return
    
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます。')
        return
    
    try:
        # 制限値チェック
        if limit < 1 or limit > 50:
            await ctx.send('❌ 表示件数は1-50の範囲で指定してください。')
            return
        
        # ボットに監査ログ表示権限があるかチェック
        bot_member = ctx.guild.me
        if not bot_member.guild_permissions.view_audit_log:
            await ctx.send('❌ ボットに監査ログを表示する権限がありません。管理者にボットに「監査ログの表示」権限を付与してもらってください。')
            return
        
        # 監査ログを取得
        audit_logs = []
        async for entry in ctx.guild.audit_logs(limit=limit):
            audit_logs.append(entry)
        
        if not audit_logs:
            await ctx.send('📋 監査ログが見つかりませんでした。')
            return
        
        # Embedメッセージを作成
        embed = discord.Embed(
            title=f"📋 監査ログ (最新{len(audit_logs)}件)",
            description=f"{ctx.guild.name}サーバーの監査ログです",
            color=discord.Color.orange()
        )
        
        # アクションタイプの日本語マッピング
        action_names = {
            discord.AuditLogAction.guild_update: "サーバー設定変更",
            discord.AuditLogAction.channel_create: "チャンネル作成",
            discord.AuditLogAction.channel_update: "チャンネル更新", 
            discord.AuditLogAction.channel_delete: "チャンネル削除",
            discord.AuditLogAction.kick: "キック",
            discord.AuditLogAction.ban: "バン追加",
            discord.AuditLogAction.unban: "バン解除",
            discord.AuditLogAction.member_update: "メンバー更新",
            discord.AuditLogAction.member_role_update: "ロール変更",
            discord.AuditLogAction.role_create: "ロール作成",
            discord.AuditLogAction.role_update: "ロール更新",
            discord.AuditLogAction.role_delete: "ロール削除",
            discord.AuditLogAction.message_delete: "メッセージ削除",
            discord.AuditLogAction.message_bulk_delete: "メッセージ一括削除",
            discord.AuditLogAction.message_pin: "メッセージピン",
            discord.AuditLogAction.message_unpin: "メッセージピン解除"
        }
        
        # ログエントリを処理
        log_entries = []
        for entry in audit_logs:
            action_name = action_names.get(entry.action, str(entry.action))
            user_name = entry.user.display_name if entry.user else "不明"
            target_name = ""
            
            if entry.target:
                if hasattr(entry.target, 'display_name'):
                    target_name = f" → {entry.target.display_name}"
                elif hasattr(entry.target, 'name'):
                    target_name = f" → {entry.target.name}"
                elif hasattr(entry.target, 'id'):
                    target_name = f" → ID:{entry.target.id}"
            
            # 時間をフォーマット
            timestamp = entry.created_at.strftime("%m/%d %H:%M")
            
            # 理由があれば追加
            reason = f"\n理由: {entry.reason}" if entry.reason else ""
            
            log_entry = f"`{timestamp}` **{action_name}**\n実行者: {user_name}{target_name}{reason}"
            log_entries.append(log_entry)
        
        # ログエントリを分割して表示（Discordの文字制限対応）
        current_field = ""
        field_count = 0
        
        for entry in log_entries:
            if len(current_field + entry + "\n\n") > 1000 or field_count >= 25:  # Discord制限
                if current_field:
                    embed.add_field(
                        name=f"📄 ログ {field_count + 1}",
                        value=current_field,
                        inline=False
                    )
                    field_count += 1
                current_field = entry + "\n\n"
            else:
                current_field += entry + "\n\n"
        
        # 最後のフィールドを追加
        if current_field:
            embed.add_field(
                name=f"📄 ログ {field_count + 1}",
                value=current_field,
                inline=False
            )
        
        embed.set_footer(
            text=f"要求者: {ctx.author.display_name} | 監査ログ表示権限が必要です",
            icon_url=ctx.author.display_avatar.url
        )
        
        await ctx.send(embed=embed)
        
        # ログ出力
        print(f"📋 監査ログ表示: {len(audit_logs)}件 | 要求者: {ctx.author}")
        
    except discord.Forbidden:
        await ctx.send('❌ ボットに監査ログへのアクセス権限がありません。管理者に権限付与を依頼してください。')
        print(f"監査ログアクセス権限不足: {ctx.guild.name}")
    except Exception as e:
        await ctx.send(f'❌ 監査ログの取得中にエラーが発生しました: {e}')
        print(f"監査ログコマンドエラー: {type(e).__name__}: {e}")

@bot.command(name='userinfo')
async def userinfo(ctx, user: Optional[discord.Member] = None):
    """
    ユーザー情報を表示するコマンド
    使用例: !userinfo (自分の情報)
    使用例: !userinfo @ユーザー (指定ユーザーの情報)
    """
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます。')
        return
    
    try:
        # ユーザーが指定されていない場合は実行者の情報を表示
        target_user = user if user else ctx.author
        
        # Embedメッセージを作成
        embed = discord.Embed(
            title="👤 ユーザー情報",
            color=discord.Color.blue()
        )
        
        # アバター画像を設定
        embed.set_thumbnail(url=target_user.display_avatar.url)
        
        # 基本情報
        embed.add_field(
            name="📛 ユーザー名",
            value=f"{target_user.name}",
            inline=True
        )
        
        embed.add_field(
            name="🆔 ユーザーID",
            value=f"`{target_user.id}`",
            inline=True
        )
        
        # ニックネーム（サーバー内表示名）
        if target_user.display_name != target_user.name:
            embed.add_field(
                name="📝 ニックネーム",
                value=f"{target_user.display_name}",
                inline=True
            )
        else:
            embed.add_field(
                name="📝 ニックネーム",
                value="設定なし",
                inline=True
            )
        
        # アカウント作成日
        created_at = target_user.created_at.strftime("%Y年%m月%d日 %H:%M")
        embed.add_field(
            name="📅 アカウント作成日",
            value=f"{created_at}",
            inline=False
        )
        
        # サーバー参加日
        if target_user.joined_at:
            joined_at = target_user.joined_at.strftime("%Y年%m月%d日 %H:%M")
            embed.add_field(
                name="🚪 サーバー参加日",
                value=f"{joined_at}",
                inline=False
            )
        
        # ステータス情報
        status_map = {
            discord.Status.online: "🟢 オンライン",
            discord.Status.idle: "🟡 アイドル",
            discord.Status.dnd: "🔴 取り込み中",
            discord.Status.offline: "⚫ オフライン"
        }
        
        status_text = status_map.get(target_user.status, "❓ 不明")
        embed.add_field(
            name="📶 ステータス",
            value=status_text,
            inline=True
        )
        
        # アクティビティ情報
        if target_user.activities:
            activity_list = []
            for activity in target_user.activities:
                if activity.type == discord.ActivityType.playing:
                    activity_list.append(f"🎮 {activity.name}")
                elif activity.type == discord.ActivityType.streaming:
                    activity_list.append(f"📺 {activity.name}")
                elif activity.type == discord.ActivityType.listening:
                    activity_list.append(f"🎵 {activity.name}")
                elif activity.type == discord.ActivityType.watching:
                    activity_list.append(f"👀 {activity.name}")
                else:
                    activity_list.append(f"📱 {activity.name}")
            
            if activity_list:
                embed.add_field(
                    name="🎯 アクティビティ",
                    value="\n".join(activity_list[:3]),  # 最大3つまで表示
                    inline=True
                )
        else:
            embed.add_field(
                name="🎯 アクティビティ",
                value="なし",
                inline=True
            )
        
        # ロール情報（@everyoneを除く）
        roles = [role for role in target_user.roles if role.name != "@everyone"]
        if roles:
            # ロールを権限の高い順にソート
            roles.sort(key=lambda x: x.position, reverse=True)
            role_names = [role.mention for role in roles[:10]]  # 最大10個まで表示
            
            role_text = ", ".join(role_names)
            if len(roles) > 10:
                role_text += f"\n... 他 {len(roles) - 10} 個のロール"
            
            embed.add_field(
                name=f"🏷️ ロール ({len(roles)}個)",
                value=role_text,
                inline=False
            )
        else:
            embed.add_field(
                name="🏷️ ロール",
                value="なし",
                inline=False
            )
        
        # 権限情報（管理者権限がある場合）
        if target_user.guild_permissions.administrator:
            embed.add_field(
                name="⚡ 権限",
                value="🔧 管理者",
                inline=True
            )
        elif target_user.guild_permissions.manage_guild:
            embed.add_field(
                name="⚡ 権限",
                value="🛠️ サーバー管理",
                inline=True
            )
        elif target_user.guild_permissions.manage_messages:
            embed.add_field(
                name="⚡ 権限",
                value="📝 メッセージ管理",
                inline=True
            )
        else:
            embed.add_field(
                name="⚡ 権限",
                value="👤 一般ユーザー",
                inline=True
            )
        
        # フッター情報
        embed.set_footer(
            text=f"要求者: {ctx.author.display_name} | 情報取得日時",
            icon_url=ctx.author.display_avatar.url
        )
        
        # タイムスタンプ
        embed.timestamp = ctx.message.created_at
        
        await ctx.send(embed=embed)
        
        # ログ出力
        print(f"👤 ユーザー情報表示: {target_user.name} (ID: {target_user.id}) | 要求者: {ctx.author}")
        
    except Exception as e:
        await ctx.send(f'❌ ユーザー情報の取得中にエラーが発生しました: {e}')
        print(f"ユーザー情報コマンドエラー: {type(e).__name__}: {e}")

@bot.command(name='antispam')
async def antispam(ctx, action: str = "status", *, value: Optional[str] = None):
    """
    スパム対策管理コマンド
    使用例: 
    !antispam status - スパム対策設定と統計を表示
    !antispam toggle - スパム対策の有効/無効を切り替え
    !antispam settings - 詳細設定を表示
    !antispam reset @ユーザー - ユーザーの警告をリセット
    !antispam unmute @ユーザー - ユーザーのミュートを解除
    !antispam stats - サーバーのスパム統計を表示
    """
    
    # 管理者権限チェック
    if not ctx.author.guild_permissions.manage_messages:
        await ctx.send('❌ このコマンドの使用にはメッセージ管理権限が必要です。')
        return
    
    if not ctx.guild:
        await ctx.send('❌ このコマンドはサーバー内でのみ使用できます。')
        return
    
    try:
        action = action.lower()
        
        if action == "status":
            # スパム対策の現在の状態を表示
            embed = discord.Embed(
                title="🛡️ スパム対策ステータス",
                color=discord.Color.green() if SPAM_SETTINGS['enabled'] else discord.Color.red()
            )
            
            status = "🟢 有効" if SPAM_SETTINGS['enabled'] else "🔴 無効"
            embed.add_field(name="現在の状態", value=status, inline=True)
            
            embed.add_field(
                name="📊 設定値",
                value=f"""
メッセージ制限: {SPAM_SETTINGS['message_limit']}件/{SPAM_SETTINGS['time_window']}秒
重複制限: {SPAM_SETTINGS['duplicate_limit']}回
警告しきい値: {SPAM_SETTINGS['warning_threshold']}回
ミュート時間: {SPAM_SETTINGS['mute_duration']}秒
                """,
                inline=False
            )
            
            # 統計情報
            stats = spam_stats[ctx.guild.id]
            embed.add_field(
                name="📈 統計情報",
                value=f"""
削除されたメッセージ: {stats['messages_deleted']}件
発行された警告: {stats['warnings_given']}回
適用されたミュート: {stats['mutes_applied']}回
                """,
                inline=False
            )
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action == "toggle":
            # スパム対策の有効/無効を切り替え
            SPAM_SETTINGS['enabled'] = not SPAM_SETTINGS['enabled']
            status = "有効" if SPAM_SETTINGS['enabled'] else "無効"
            color = discord.Color.green() if SPAM_SETTINGS['enabled'] else discord.Color.red()
            
            embed = discord.Embed(
                title="🛡️ スパム対策設定変更",
                description=f"スパム対策を **{status}** にしました。",
                color=color
            )
            await ctx.send(embed=embed)
            
        elif action == "settings":
            # 詳細設定を表示
            embed = discord.Embed(
                title="⚙️ スパム対策詳細設定",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="検出条件",
                value=f"""
🔄 **短時間大量投稿**: {SPAM_SETTINGS['time_window']}秒間で{SPAM_SETTINGS['message_limit']}件以上
🔁 **重複メッセージ**: 同じ内容を{SPAM_SETTINGS['duplicate_limit']}回連続
⚠️ **警告しきい値**: {SPAM_SETTINGS['warning_threshold']}回でミュート
🔇 **ミュート時間**: {SPAM_SETTINGS['mute_duration']}秒 ({SPAM_SETTINGS['mute_duration']//60}分)
                """,
                inline=False
            )
            
            embed.add_field(
                name="除外対象",
                value="• ボットユーザー\n• 管理者権限所持者\n• コマンドメッセージ (!で開始)",
                inline=False
            )
            
            embed.set_footer(text="設定値の変更は開発者にお問い合わせください")
            await ctx.send(embed=embed)
            
        elif action == "reset":
            # ユーザーの警告をリセット
            if not value:
                await ctx.send('❌ リセットするユーザーを指定してください。\n使用例: `!antispam reset @ユーザー`')
                return
            
            # メンションからユーザーを取得
            member = None
            if ctx.message.mentions:
                member = ctx.message.mentions[0]
            else:
                # IDで検索
                try:
                    user_id = int(value.strip('<@!>'))
                    member = ctx.guild.get_member(user_id)
                except ValueError:
                    await ctx.send('❌ 有効なユーザーを指定してください。')
                    return
            
            if not member:
                await ctx.send('❌ ユーザーが見つかりませんでした。')
                return
            
            # 警告をリセット
            old_warnings = user_warnings[ctx.guild.id][member.id]
            user_warnings[ctx.guild.id][member.id] = 0
            
            embed = discord.Embed(
                title="🔄 警告リセット完了",
                description=f"{member.mention} の警告をリセットしました。",
                color=discord.Color.green()
            )
            embed.add_field(name="以前の警告回数", value=f"{old_warnings}回", inline=True)
            embed.add_field(name="現在の警告回数", value="0回", inline=True)
            await ctx.send(embed=embed)
            
        elif action == "unmute":
            # ユーザーのミュートを手動解除
            if not value:
                await ctx.send('❌ ミュート解除するユーザーを指定してください。\n使用例: `!antispam unmute @ユーザー`')
                return
            
            # メンションからユーザーを取得
            member = None
            if ctx.message.mentions:
                member = ctx.message.mentions[0]
            else:
                try:
                    user_id = int(value.strip('<@!>'))
                    member = ctx.guild.get_member(user_id)
                except ValueError:
                    await ctx.send('❌ 有効なユーザーを指定してください。')
                    return
            
            if not member:
                await ctx.send('❌ ユーザーが見つかりませんでした。')
                return
            
            # Mutedロールを取得
            mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
            if not mute_role:
                await ctx.send('❌ Mutedロールが見つかりません。')
                return
            
            if mute_role not in member.roles:
                await ctx.send(f'❌ {member.mention} はミュートされていません。')
                return
            
            try:
                await member.remove_roles(mute_role, reason=f"管理者による手動ミュート解除 ({ctx.author})")
                user_warnings[ctx.guild.id][member.id] = 0  # 警告もリセット
                
                embed = discord.Embed(
                    title="🔊 ミュート解除完了",
                    description=f"{member.mention} のミュートを解除しました。",
                    color=discord.Color.green()
                )
                embed.add_field(name="実行者", value=ctx.author.mention, inline=True)
                await ctx.send(embed=embed)
                
            except discord.Forbidden:
                await ctx.send('❌ ミュート解除に必要な権限がありません。')
            except Exception as e:
                await ctx.send(f'❌ ミュート解除中にエラーが発生しました: {e}')
                
        elif action == "stats":
            # 詳細統計を表示
            stats = spam_stats[ctx.guild.id]
            embed = discord.Embed(
                title="📊 スパム対策統計",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="処理済み件数",
                value=f"""
🗑️ 削除メッセージ: **{stats['messages_deleted']}** 件
⚠️ 発行警告: **{stats['warnings_given']}** 回
🔇 適用ミュート: **{stats['mutes_applied']}** 回
                """,
                inline=False
            )
            
            # 現在警告中のユーザー数
            warned_users = sum(1 for warnings in user_warnings[ctx.guild.id].values() if warnings > 0)
            embed.add_field(
                name="現在の状況",
                value=f"⚠️ 警告中ユーザー: **{warned_users}** 人",
                inline=True
            )
            
            # Mutedロールを持つユーザー数
            mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
            muted_users = len(mute_role.members) if mute_role else 0
            embed.add_field(
                name="ミュート中",
                value=f"🔇 ミュート中ユーザー: **{muted_users}** 人",
                inline=True
            )
            
            embed.set_footer(text=f"要求者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        else:
            # 無効なアクション
            await ctx.send(f'❌ 無効なアクションです: `{action}`\n使用可能: status, toggle, settings, reset, unmute, stats')
            
    except Exception as e:
        await ctx.send(f'❌ スパム対策コマンドの実行中にエラーが発生しました: {e}')
        print(f"スパム対策コマンドエラー: {type(e).__name__}: {e}")

# エラーハンドリング
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # コマンドが見つからない場合は無視
    
    print(f'エラーが発生しました: {error}')
    await ctx.send('❌ コマンドの実行中にエラーが発生しました')

if __name__ == '__main__':
    # 環境変数からトークンを取得
    token = os.getenv('DISCORD_BOT_TOKEN')
    if token:
        bot.run(token)
    else:
        print("❌ DISCORD_BOT_TOKENが設定されていません")
        print("環境変数にDiscordボットのトークンを設定してください")