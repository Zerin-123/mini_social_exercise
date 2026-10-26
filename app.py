from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = '123456789'
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted.
# If you want to test whether moderation works,
# you can trigger censorship using these words:
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']


def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def ensure_reaction_timestamp():
    """
    "Active Today" (C2-Claim 12) must know WHEN a reaction was made.
    If the reactions table has no created_at column yet, add it once at startup.
    (SQLite cannot add a column with a non-constant default, so the value is
    set explicitly in add_reaction() below.)
    """
    conn = sqlite3.connect(DATABASE)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(reactions)").fetchall()]
        if cols and 'created_at' not in cols:
            conn.execute("ALTER TABLE reactions ADD COLUMN created_at TIMESTAMP")
            conn.commit()
            print("Migration: added created_at column to reactions table")
    finally:
        conn.close()


ensure_reaction_timestamp()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()

    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful,
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)

        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv

        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None


@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            dt = datetime.fromisoformat(value)
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')


REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()

    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    needs_attention = []
    needs_attention_total = 0
    top_contributors = []
    active_today = []
    my_activity = None

    db = get_db()

    # --- STEP 1: C2 Claim 1 + C2 Claim 12 (Needs Your Contribution, with social proof) ---
    # Design Claim 1: making the list of needed contributions easily visible.
    # "Contribution" = reacting to or commenting on a post by someone you follow.
    # Design Claim 12: people comply more when they see that others complied,
    # so every pending item also shows how many reactions/comments it already has.
    if current_user_id:
        try:
            # Posts from followed users that the current user has NOT yet
            # reacted to AND NOT yet commented on.
            pending_from = """
                FROM posts p
                JOIN follows f ON p.user_id = f.followed_id
                JOIN users u ON p.user_id = u.id
                WHERE f.follower_id = ?
                  AND p.user_id != ?
                  AND NOT EXISTS (SELECT 1 FROM reactions r WHERE r.post_id = p.id AND r.user_id = ?)
                  AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = ?)
            """
            args = (current_user_id, current_user_id, current_user_id, current_user_id)

            # Total number of pending contributions (shown as a counter badge)
            needs_attention_total = db.execute("SELECT COUNT(*) " + pending_from, args).fetchone()[0]

            rows = db.execute(
                """SELECT p.id, p.content, u.username,
                          (SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) AS reaction_count,
                          (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count """
                + pending_from + " ORDER BY p.created_at DESC LIMIT 20", args).fetchall()

            for row in rows:
                item = dict(row)
                # Censor the text (moderate_content) so the widget never shows
                # content that is hidden elsewhere on the site.
                item['content'], _ = moderate_content(item['content'])
                if item['content'] == "[removed]":
                    continue  # do not ask people to contribute to removed posts
                needs_attention.append(item)
                if len(needs_attention) == 5:
                    break
        except Exception as e:
            print(f"C2-1 Error: {e}")
            needs_attention = []
            needs_attention_total = 0

    # --- STEP 2: C2 Claim 12 (Active Today) ---
    # Shows the users who were most active TODAY only, counting what they
    # DID today: posts written, comments written and reactions MADE by them.
    # Timestamps are stored in UTC, so both sides are converted to local time
    # ('localtime') to make "today" match the user's own day.
    try:
        # The reactions table needs a created_at column to know WHEN a reaction was made.
        reaction_cols = [row[1] for row in db.execute("PRAGMA table_info(reactions)").fetchall()]
        if 'created_at' in reaction_cols:
            reactions_today_sql = ("(SELECT COUNT(*) FROM reactions r WHERE r.user_id = u.id "
                                   "AND date(r.created_at,'localtime') = date('now','localtime'))")
        else:
            print("C2-12 Warning: reactions table has no created_at column")
            reactions_today_sql = "0"

        rows = db.execute(f"""
            SELECT user_id, username, posts, comments, reactions FROM (
                SELECT u.id AS user_id, u.username AS username,
                       (SELECT COUNT(*) FROM posts p
                         WHERE p.user_id = u.id
                           AND date(p.created_at,'localtime') = date('now','localtime')) AS posts,
                       (SELECT COUNT(*) FROM comments c
                         WHERE c.user_id = u.id
                           AND date(c.created_at,'localtime') = date('now','localtime')) AS comments,
                       {reactions_today_sql} AS reactions
                FROM users u
            )
            WHERE posts + comments + reactions > 0
            ORDER BY (posts + comments + reactions) DESC, username ASC
        """).fetchall()
        all_active = [{'user_id': r[0], 'username': r[1], 'posts': r[2],
                       'comments': r[3], 'reactions': r[4]} for r in rows]
        active_today = all_active[:5]  # only the top 5 are displayed

        # Comparison (inspired by Claim 13's "compare your activity to others"):
        # the logged-in user's contributions today vs. the average of everyone active today.
        if current_user_id:
            totals = [a['posts'] + a['comments'] + a['reactions'] for a in all_active]
            my_total = next((a['posts'] + a['comments'] + a['reactions']
                             for a in all_active if a['user_id'] == current_user_id), 0)
            average = (sum(totals) / len(totals)) if totals else 0
            my_activity = {
                'total': my_total,
                'average': round(average, 1),
                'active_count': len(totals),
                # progress bar width in % (capped at 100)
                'percent': min(100, round(my_total / average * 100)) if average else 0,
            }

    except Exception as e:
        print(f"C2-12 Error: {e}")
        active_today = []

    # --- STEP 3: C3 Claim 15 - Info + Recent Activities of individual members ---
    # Everything comes from the database (nothing hardcoded).
    try:
        c3_15 = db.cursor()
        c3_15.execute("""
            SELECT u.username, u.id, COUNT(p.id) as total_posts, 
                   MAX(p.created_at) as last_active,
                   (SELECT p2.content FROM posts p2 WHERE p2.user_id = u.id ORDER BY p2.created_at DESC LIMIT 1) as recent_activity
            FROM users u
            JOIN posts p ON p.user_id = u.id
            GROUP BY u.id, u.username
            ORDER BY total_posts DESC LIMIT 5
        """)
        rows = c3_15.fetchall()
        top_contributors = []
        for r in rows:
            # r[0]=username, r[1]=id (for avatar colour), r[2]=count, r[3]=last_active, r[4]=recent post
            recent_clean, _ = moderate_content(r[4] or '')
            # Claim 15 made interactive: can the viewer follow this member right away?
            is_me = (current_user_id == r[1])
            is_following = False
            if current_user_id and not is_me:
                is_following = db.execute(
                    'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                    (current_user_id, r[1])).fetchone() is not None
            top_contributors.append(
                {'username': r[0], 'user_id': r[1], 'total_posts': r[2], 'last_active': r[3],
                 'recent_activity': recent_clean, 'is_me': is_me, 'is_following': is_following})

    except Exception as e:
        print(f"C3-15 Error: {e}")
        top_contributors = []


    #  2. Build the Query
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params) or []

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db(
            'SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type',
            (post['id'],))
        comments_raw = query_db(
            'SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC',
            (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    # --- Coding Assignment #3: newcomer features (Design Claims 18 and 19) ---
    welcome_desk = []
    newcomer_panel = None
    if current_user_id:
        try:
            welcome_desk = get_welcome_desk(current_user_id)
            newcomer_panel = get_newcomer_panel(current_user_id)
        except Exception as e:
            print(f"Newcomer features error: {e}")

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2',
                           posts=posts_data,
                           current_sort=sort,
                           current_show=show,
                           page=page,  # Pass current page number
                           per_page=POSTS_PER_PAGE,  # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           needs_attention=needs_attention,
                           needs_attention_total=needs_attention_total,
                           active_today=active_today,
                           my_activity=my_activity,
                           top_contributors=top_contributors,
                           welcome_desk=welcome_desk,
                           newcomer_panel=newcomer_panel,
                           newcomer_days=NEWCOMER_DAYS,
                           welcome_max_length=WELCOME_MAX_LENGTH)




@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Design Claim 20: remind the user at the point of action if the post would
    # trigger a moderation rule; they can edit it, post anyway, or cancel.
    if content and content.strip():
        reminder = norm_reminder(content, url_for('add_post'), 'post', 'post', url_for('feed'))
        if reminder:
            return reminder

    # The content is stored as typed; moderate_content() is applied when it is displayed
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute("INSERT INTO posts (user_id, content, created_at) VALUES (?, ?, datetime('now'))",
                   (user_id, moderated_content))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))


@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""

    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db(
        'SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db(
        'SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100',
        (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)[
        'cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)[
        'cnt']

    #  NEW: CHECK FOLLOW STATUS
    is_currently_following = False  # Default to False
    current_user_id = session.get('user_id')

    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2',
                           user=user,
                           posts=posts,
                           comments=comments,
                           followers_count=followers_count,
                           following_count=following_count,
                           is_following=is_currently_following)


@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")


@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")


@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""

    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed)
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments
    comments_raw = query_db(
        'SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC',
        (post_id,))

    comments = []  # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw)  # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)


@app.route('/about')
def about():
    return render_template('about.html.j2')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                "INSERT INTO users (username, password, location, birthdate, profile, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear()  # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created. Tell the community about yourself below.', 'success')
            # Design Claim 19: newcomers land on the introduction thread first
            return redirect(url_for('introductions_page'))

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()

    return render_template('signup.html.j2')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')

    return render_template('login.html.j2')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Design Claim 20: reminder at the point of action (see norm_reminder)
    if content and content.strip():
        reminder = norm_reminder(content, url_for('add_comment', post_id=post_id), 'comment',
                                 'comment', url_for('post_detail', post_id=post_id))
        if reminder:
            return reminder

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute("INSERT INTO comments (post_id, user_id, content, created_at) VALUES (?, ?, ?, datetime('now'))",
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # 'next' is set when the comment came through the reminder page (Design Claim 20)
    return redirect(_safe_next_url(request.form.get('next')) or request.referrer or url_for('post_detail', post_id=post_id))


@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))


@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        # created_at is refreshed so a changed reaction counts as activity today
        db.execute("UPDATE reactions SET reaction_type = ?, created_at = datetime('now') WHERE id = ?",
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        # created_at is stored explicitly (UTC) so "Active Today" can filter by date
        db.execute("INSERT INTO reactions (post_id, user_id, reaction_type, created_at) VALUES (?, ?, ?, datetime('now'))",
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))


@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id, username FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = user_to_follow['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    user_to_unfollow = query_db('SELECT id, username FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_unfollow:
        flash("The user does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
                     (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = user_to_unfollow['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))


@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1

    current_tab = request.args.get('tab', 'users')  # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE

    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset: users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score
        author_created_dt = post_dict['user_created_at']
        if isinstance(author_created_dt, str):
            try:
                author_created_dt = datetime.fromisoformat(author_created_dt)
            except ValueError:
                author_created_dt = datetime.utcnow()
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True)  # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        if isinstance(author_created_dt, str):
            try:
                author_created_dt = datetime.fromisoformat(author_created_dt)
            except ValueError:
                author_created_dt = datetime.utcnow()
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True)  # Sort after fetching and scoring

    return render_template('admin.html.j2',
                           users=users,
                           posts=posts,
                           comments=comments,

                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)


@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))

    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/rules')
def rules():
    return render_template('rules.html.j2')


@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# ---------------------------------------------------------------------------
# Coding Assignment #2 - Moderation
# Rules: https://mini-social.szab.eu/rules  (Part 1 = content, Part 2 = risk scores)
# All word lists come from censorship.dat (TIER1_WORDS, TIER2_PHRASES, TIER3_WORDS).
# Nothing below is hardcoded per user, per word or per id.
# ---------------------------------------------------------------------------

# Fixed values taken directly from the rules page
TIER1_REMOVED_TEXT = "[content removed due to severe violation]"   # Rule 1.1.1
TIER2_REMOVED_TEXT = "[content removed due to spam/scam policy]"   # Rule 1.1.2
LINK_REMOVED_TEXT = "[link removed]"                                # Rule 1.2.2
TIER3_MASK = "*****"         # Exercise 5 slides 12 & 20: Tier 3 word -> "*****"
SEVERE_SCORE = 5.0           # Rules 1.1.1 / 1.1.2
TIER3_SCORE = 2.0            # Rule 1.2.1 (per match)
LINK_SCORE = 2.0             # Rule 1.2.2 (per match)
CAPS_SCORE = 0.5             # Rule 1.2.3
CAPS_MIN_LETTERS = 15        # Rule 1.2.3: more than 15 alphabetic characters
CAPS_MIN_RATIO = 0.7         # Rule 1.2.3: more than 70% uppercase
MAX_USER_RISK = 5.0          # Rule 2.2 step 6

URL_PATTERN = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)


def _build_pattern(items):
    """
    Builds ONE case-insensitive regex that matches any of the given words/phrases
    as a WHOLE word / whole phrase (not inside a longer word).
    Longest entries are tried first so that long phrases win over short ones.
    """
    cleaned = sorted({i.strip() for i in items if i and i.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    body = '|'.join(re.escape(i) for i in cleaned)
    return re.compile(r'(?<!\w)(?:' + body + r')(?!\w)', re.IGNORECASE)


# Compiled once when the app starts (fast, and re-used for every request)
TIER1_PATTERN = _build_pattern(TIER1_WORDS)
TIER2_PATTERN = _build_pattern(TIER2_PHRASES)
TIER3_PATTERN = _build_pattern(TIER3_WORDS)


def analyze_content(text):
    """
    Applies the content moderation rules (rules page, Part 1).
    Returns (filtered_text, content_score, reasons)
    'reasons' is a list of human-readable rule descriptions that were triggered.
    It is used by the point-of-action reminder (Design Claim 20).
    """
    if not text:
        return "", 0.0, []

    # Stage 1.1 - severe violations: whole content removed, fixed score 5.0
    if TIER1_PATTERN and TIER1_PATTERN.search(text):
        return TIER1_REMOVED_TEXT, SEVERE_SCORE, ["severe violation (Tier 1 word)"]
    if TIER2_PATTERN and TIER2_PATTERN.search(text):
        return TIER2_REMOVED_TEXT, SEVERE_SCORE, ["spam/scam phrase (Tier 2)"]

    # Stage 1.2 - scored violations
    filtered = text
    score = 0.0
    reasons = []

    # Rule 1.2.1 (Exercise 5, slides 12/20): every mild-profanity match -> "*****", +2.0 each
    if TIER3_PATTERN:
        filtered, n_words = TIER3_PATTERN.subn(lambda m: TIER3_MASK, filtered)
        if n_words:
            score += TIER3_SCORE * n_words
            reasons.append(f"{n_words} mild profanity word(s) will be masked with *****")

    # Rule 1.2.2: every URL -> "[link removed]", +2.0 each
    filtered, n_links = URL_PATTERN.subn(LINK_REMOVED_TEXT, filtered)
    if n_links:
        score += LINK_SCORE * n_links
        reasons.append(f"{n_links} link(s) will be removed")

    # Rule 1.2.3: more than 15 letters and more than 70% uppercase -> +0.5 (text unchanged)
    letters = [c for c in text if c.isalpha()]
    if len(letters) > CAPS_MIN_LETTERS:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > CAPS_MIN_RATIO:
            score += CAPS_SCORE
            reasons.append("mostly CAPITAL LETTERS (looks like shouting)")

    return filtered, score, reasons


def moderate_content(text):
    """
    Returns (filtered_text, content_score) for a post, comment or profile text.
    See analyze_content() for the rules that are applied.
    """
    filtered, score, _ = analyze_content(text)
    return filtered, score


def _account_age_days(created_at):
    """Account age in whole days (None if unknown). created_at is stored in UTC."""
    if not created_at:
        return None
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc - created_at).days


def user_risk_analysis(user_id):
    """
    User risk score (rules page, Rule 2.2):
      content_risk = profile_score*1 + average_post_score*3 + average_comment_score*1
      x1.5 if account < 7 days, x1.2 if account < 30 days, capped at 5.0
    """
    try:
        user = query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
        if not user:
            return 0.0

        # Step 1 - profile score (the profile text column is called "profile")
        profile_text = user['profile'] if 'profile' in user.keys() else ''
        _, profile_score = moderate_content(profile_text or '')

        # Step 2 - average post score (0 if the user has no posts)
        posts = query_db("SELECT content FROM posts WHERE user_id = ?", (user_id,)) or []
        post_scores = [moderate_content(p['content'])[1] for p in posts]
        average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0

        # Step 3 - average comment score (0 if the user has no comments)
        comments = query_db("SELECT content FROM comments WHERE user_id = ?", (user_id,)) or []
        comment_scores = [moderate_content(c['content'])[1] for c in comments]
        average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

        # Step 4 - weighted combination (posts count most)
        content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

        # Step 5 - account age multiplier
        age_days = _account_age_days(user['created_at'] if 'created_at' in user.keys() else None)
        if age_days is not None and age_days < 7:
            user_risk_score = content_risk_score * 1.5
        elif age_days is not None and age_days < 30:
            user_risk_score = content_risk_score * 1.2
        else:
            user_risk_score = content_risk_score

        # Step 6 - final cap
        return min(MAX_USER_RISK, user_risk_score)

    except Exception as e:
        print(f"user_risk_analysis error: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Design Claim 20 (Chapter 4): "Offering people reminders at the point of an
# action that may violate norms reduces the number of offenses."
#
# When a user submits a post or comment that the moderation rules would censor
# or flag, the content is NOT saved straight away. Instead the user sees a
# reminder that lists which rules would be triggered, how the text would look
# after moderation, and can: edit the text and re-check it, post it anyway, or
# cancel. Clean content is saved immediately, as before.
# ---------------------------------------------------------------------------
REMINDER_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Before you {{ action_word }} - Mini Social</title>
  <style>
    body { font-family: system-ui, sans-serif; background:#f4f5f7; margin:0; padding:24px; }
    .box { max-width:640px; margin:0 auto; background:#fff; border:2px solid #f0ad4e;
           border-radius:10px; padding:24px; }
    h2 { margin-top:0; }
    .preview { background:#f8f9fa; border-left:4px solid #6c757d; padding:10px 14px;
               border-radius:4px; white-space:pre-wrap; }
    textarea { width:100%; box-sizing:border-box; min-height:110px; padding:8px; font:inherit; }
    .row { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; align-items:center; }
    button, a.cancel { padding:8px 16px; border-radius:6px; border:1px solid #888;
                       font:inherit; cursor:pointer; text-decoration:none; color:#000; background:#eee; }
    button.primary { background:#0d6efd; color:#fff; border-color:#0d6efd; }
    button.warn { background:#fff; color:#b02a37; border-color:#b02a37; }
  </style>
</head>
<body>
  <div class="box">
    <h2>&#9888; Before you {{ action_word }}...</h2>
    <p>Our community rules would act on this {{ item_word }}:</p>
    <ul>
      {% for r in reasons %}<li>{{ r }}</li>{% endfor %}
    </ul>
    <p><b>How it would appear to others:</b></p>
    <div class="preview">{{ preview }}</div>
    <p style="margin-top:18px"><b>Want to edit it first?</b></p>
    <form method="POST" action="{{ action_url }}">
      <textarea name="content" required>{{ content }}</textarea>
      {% if next_url %}<input type="hidden" name="next" value="{{ next_url }}">{% endif %}
      <div class="row">
        <button type="submit" class="primary">Check my edit</button>
        <button type="submit" name="confirm_submit" value="1" class="warn">{{ action_word|capitalize }} anyway</button>
        <a class="cancel" href="{{ cancel_url }}">Cancel</a>
      </div>
    </form>
    <p style="font-size:12px;color:#666">See the full <a href="{{ url_for('rules') }}">community rules</a>.</p>
  </div>
</body>
</html>
"""


def _safe_next_url(url):
    """Only allow redirects back to this same site (prevents open redirects)."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    return url


def norm_reminder(content, action_url, action_word, item_word, fallback_url):
    """
    Design Claim 20 helper. Returns a rendered reminder page when the content would
    trigger a moderation rule and the user has not confirmed yet, otherwise None.
    """
    filtered, _score, reasons = analyze_content(content)
    if not reasons or request.form.get('confirm_submit') == '1':
        return None
    next_url = _safe_next_url(request.form.get('next') or request.referrer)
    return render_template_string(
        REMINDER_TEMPLATE,
        reasons=reasons, preview=filtered, content=content,
        action_url=action_url, action_word=action_word, item_word=item_word,
        next_url=next_url, cancel_url=next_url or fallback_url)


# ---------------------------------------------------------------------------
# Coding Assignment #3 - Dealing with newcomers
#   * recommend()                  -> "Recommended" tab (5 posts)
#   * Chapter 5, Design Claim 19   -> introduction thread (/introductions)
#   * Chapter 5, Design Claim 18   -> Welcome Desk (personal welcomes from old-timers)
# Nothing here is hardcoded per user or per post: everything is read from the database.
# ---------------------------------------------------------------------------

# --- Settings (tunable numbers, not data) -----------------------------------
NEWCOMER_DAYS = 7              # same 7-day limit that the moderation rules use for "new accounts"
NEGATIVE_REACTIONS = ('sad', 'angry')   # every other reaction type counts as a positive reaction
RECOMMEND_COUNT = 5            # the assignment asks for 5 recommended posts
RECOMMEND_CANDIDATES = 200     # how many recent posts are scored
TOP_INTEREST_WORDS = 10        # how many keywords describe a user's interests
W_FOLLOWED_AUTHOR = 3.0        # author is followed by the user
W_LIKED_AUTHOR = 1.5           # per earlier positive reaction to the same author ...
MAX_LIKED_AUTHOR_HITS = 3      # ... counted at most this many times
W_KEYWORD = 1.0                # per interest keyword shared with posts the user liked ...
MAX_SHARED_KEYWORDS = 5        # ... counted at most this many times
W_POPULARITY = 0.5             # per positive reaction the post already has ...
MAX_POPULARITY_HITS = 4        # ... counted at most this many times
WELCOME_DESK_SIZE = 5          # newcomers shown on the Welcome Desk
WELCOME_MAX_LENGTH = 300       # longest welcome message
INTRO_LIST_SIZE = 50           # introductions shown on the introductions page
KEYWORD_MIN_LENGTH = 4         # words shorter than this are ignored as keywords

# Prompts shown to newcomers to make self-disclosure easy (Design Claim 19)
INTRO_PROMPTS = [
    "Who are you and where are you from?",
    "What brings you to MiniSocial?",
    "What do you like to talk about or make?",
]

KEYWORD_STOPWORDS = {
    'about', 'after', 'again', 'also', 'because', 'been', 'being', 'could', 'does', 'from',
    'have', 'here', 'into', 'just', 'like', 'make', 'more', 'much', 'only', 'other', 'really',
    'should', 'some', 'than', 'that', 'their', 'them', 'then', 'there', 'these', 'they', 'this',
    'very', 'want', 'were', 'what', 'when', 'where', 'which', 'while', 'will', 'with', 'would',
    'your', 'hello', 'today', 'love', 'good', 'great', 'welcome',
}


def ensure_newcomer_schema():
    """
    Small migration run at startup:
      * posts.is_intro  - marks a post as an introduction (Design Claim 19)
      * welcomes table  - remembers who welcomed which newcomer (Design Claim 18)
    """
    conn = sqlite3.connect(DATABASE)
    try:
        post_cols = [row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()]
        if post_cols and 'is_intro' not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN is_intro INTEGER NOT NULL DEFAULT 0")
            print("Migration: added is_intro column to posts table")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS welcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                newcomer_id INTEGER NOT NULL,
                welcomer_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                comment_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (newcomer_id, welcomer_id)
            )""")
        conn.commit()
    finally:
        conn.close()


ensure_newcomer_schema()


def _keywords(text):
    """Lower-case content words of a text (used to describe interests)."""
    words = re.findall(r"[a-z']{%d,}" % KEYWORD_MIN_LENGTH, (text or '').lower())
    return [w for w in words if w not in KEYWORD_STOPWORDS]


def _positive_reaction_types():
    return [t for t in REACTION_TYPES if t not in NEGATIVE_REACTIONS]


# ---------------------------------------------------------------------------
# recommend(): content + social recommendation, based on
#   (1) the posts the user reacted to positively  and  (2) the users they follow.
# Every candidate post gets a score:
#   + W_FOLLOWED_AUTHOR   if the user follows its author
#   + W_LIKED_AUTHOR      for each earlier positive reaction to the same author
#   + W_KEYWORD           for each interest keyword (taken from liked posts) it contains
#   + W_POPULARITY        for each positive reaction the post already has
# The 5 best posts are returned (newest wins ties). With no history at all (new user)
# only popularity is left, so newcomers see the community's popular posts.
# ---------------------------------------------------------------------------
def recommend(user_id, filter_following):
    positive = _positive_reaction_types()
    marks = ','.join('?' * len(positive))
    base_select = (
        "SELECT p.id, p.content, p.created_at, p.user_id, u.username, "
        f"(SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id AND r.reaction_type IN ({marks})) AS popularity "
        "FROM posts p JOIN users u ON p.user_id = u.id")

    try:
        # Not logged in: nothing is known about the visitor -> most popular posts
        if not user_id:
            rows = query_db(base_select + " ORDER BY popularity DESC, p.created_at DESC LIMIT ?",
                            positive + [RECOMMEND_COUNT]) or []
            return [dict(r, reason="Popular in the community") for r in rows]

        # (1) Interests: authors and keywords of posts this user reacted to positively
        liked = query_db(
            "SELECT p.user_id, p.content FROM reactions r JOIN posts p ON p.id = r.post_id "
            f"WHERE r.user_id = ? AND r.reaction_type IN ({marks})", [user_id] + positive) or []
        liked_authors = collections.Counter(r['user_id'] for r in liked if r['user_id'] != user_id)
        keyword_counts = collections.Counter()
        for r in liked:
            keyword_counts.update(set(_keywords(r['content'])))
        interests = {w for w, _ in keyword_counts.most_common(TOP_INTEREST_WORDS)}

        # (2) Social ties: the users this user follows
        followed = {r['followed_id'] for r in
                    (query_db("SELECT followed_id FROM follows WHERE follower_id = ?", (user_id,)) or [])}

        # Candidates: recent posts by other people that the user has not reacted to yet
        where = ("WHERE p.user_id != ? "
                 "AND p.id NOT IN (SELECT post_id FROM reactions WHERE user_id = ?)")
        params = positive + [user_id, user_id]
        if filter_following:
            where += " AND p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
            params.append(user_id)
        candidates = query_db(base_select + " " + where + " ORDER BY p.created_at DESC LIMIT ?",
                              params + [RECOMMEND_CANDIDATES]) or []

        scored = []
        for c in candidates:
            score = 0.0
            reasons = []
            if c['user_id'] in followed:
                score += W_FOLLOWED_AUTHOR
                reasons.append(f"you follow @{c['username']}")
            hits = liked_authors.get(c['user_id'], 0)
            if hits:
                score += W_LIKED_AUTHOR * min(hits, MAX_LIKED_AUTHOR_HITS)
                reasons.append(f"you liked @{c['username']}'s posts")
            shared = sorted(interests & set(_keywords(c['content'])))
            if shared:
                score += W_KEYWORD * min(len(shared), MAX_SHARED_KEYWORDS)
                reasons.append("it is similar to posts you liked (" + ", ".join(shared[:3]) + ")")
            score += W_POPULARITY * min(c['popularity'], MAX_POPULARITY_HITS)

            if reasons:
                reason = "Because " + " and ".join(reasons[:2])
            elif c['popularity']:
                reason = "Popular in the community"
            else:
                reason = "New in the community"
            scored.append((score, str(c['created_at']), dict(c, reason=reason)))

        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s[2] for s in scored[:RECOMMEND_COUNT]]

    except Exception as e:
        print(f"recommend error: {e}")
        return []


# ---------------------------------------------------------------------------
# Newcomer helpers
# ---------------------------------------------------------------------------
def is_newcomer(created_at):
    """A newcomer is an account younger than NEWCOMER_DAYS days."""
    age = _account_age_days(created_at)
    return age is not None and age < NEWCOMER_DAYS


def suggest_welcome_message(username, location, intro_text):
    """
    Builds a PERSONAL welcome draft from what the newcomer shared (location and the
    topic they mention most). Personal messages work better than generic templates
    (Chapter 5); "we" language strengthens the effect (Lecture 6). The old-timer can edit it.
    """
    parts = [f"Hi {username}, welcome to MiniSocial! We're glad you joined us. 👋"]
    if location:
        parts.append(f"It's nice to see someone from {location}.")
    words = _keywords(intro_text)
    if words:
        topic = collections.Counter(words).most_common(1)[0][0]
        parts.append(f'I noticed you mentioned "{topic}" - what got you into it?')
    else:
        parts.append("What brings you here?")
    return " ".join(parts)


def get_welcome_desk(user_id):
    """
    Design Claim 18: newcomers an ESTABLISHED member can greet right now.
    Newest and least-welcomed newcomers come first, so nobody is left without a reply.
    """
    me = query_db("SELECT created_at FROM users WHERE id = ?", (user_id,), one=True)
    if not me or is_newcomer(me['created_at']):
        return []   # only established members act as welcomers
    rows = query_db("""
        SELECT u.id, u.username, u.location,
               (SELECT p.id FROM posts p WHERE p.user_id = u.id
                  ORDER BY p.is_intro DESC, p.created_at ASC LIMIT 1) AS post_id,
               (SELECT p.content FROM posts p WHERE p.user_id = u.id
                  ORDER BY p.is_intro DESC, p.created_at ASC LIMIT 1) AS post_content,
               (SELECT COUNT(*) FROM welcomes w WHERE w.newcomer_id = u.id) AS welcome_count
        FROM users u
        WHERE (julianday('now') - julianday(u.created_at)) < ?
          AND u.id != ?
          AND u.id NOT IN (SELECT newcomer_id FROM welcomes WHERE welcomer_id = ?)
          AND EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id)
        ORDER BY welcome_count ASC, u.created_at DESC
        LIMIT ?
    """, (NEWCOMER_DAYS, user_id, user_id, WELCOME_DESK_SIZE)) or []
    desk = []
    for r in rows:
        shown, _ = moderate_content(r['post_content'] or '')
        removed = shown != (r['post_content'] or '')
        desk.append({
            'id': r['id'], 'username': r['username'], 'location': r['location'],
            'post_id': r['post_id'], 'welcome_count': r['welcome_count'],
            'preview': (shown[:80] + ('...' if len(shown) > 80 else '')),
            'suggestion': suggest_welcome_message(
                r['username'], r['location'], '' if removed else r['post_content']),
        })
    return desk


def get_newcomer_panel(user_id):
    """Design Claims 18/19, newcomer side: onboarding checklist + who has welcomed me."""
    me = query_db("SELECT created_at FROM users WHERE id = ?", (user_id,), one=True)
    if not me or not is_newcomer(me['created_at']):
        return None
    has_intro = query_db("SELECT 1 FROM posts WHERE user_id = ? AND is_intro = 1 LIMIT 1",
                         (user_id,), one=True) is not None
    rows = query_db("""
        SELECT u.username, w.post_id, c.content
        FROM welcomes w JOIN users u ON u.id = w.welcomer_id
        LEFT JOIN comments c ON c.id = w.comment_id
        WHERE w.newcomer_id = ? ORDER BY w.created_at DESC
    """, (user_id,)) or []
    welcomes = [{'username': r['username'], 'post_id': r['post_id'],
                 'content': moderate_content(r['content'] or '')[0]} for r in rows]
    return {'has_intro': has_intro, 'welcomes': welcomes}


# ---------------------------------------------------------------------------
# Design Claim 19 - introduction thread
# "Encouraging newcomers to reveal themselves publicly in profiles or introduction
#  threads gives existing group members a basis for conversation and reciprocation."
# ---------------------------------------------------------------------------
@app.route('/introductions', methods=['GET', 'POST'])
def introductions_page():
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to see and write introductions.', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':
        content = (request.form.get('content') or '').strip()
        if not content:
            flash('Please write a few words about yourself.', 'warning')
        else:
            # Claim 20 (Coding #2): remind the user if the text would be censored
            reminder = norm_reminder(content, url_for('introductions_page'), 'post',
                                     'introduction', url_for('introductions_page'))
            if reminder:
                return reminder
            db = get_db()
            db.execute('INSERT INTO posts (user_id, content, is_intro) VALUES (?, ?, 1)',
                       (user_id, content))
            db.commit()
            flash('Thanks for introducing yourself! Members can now say hello.', 'success')
            return redirect(url_for('introductions_page'))

    rows = query_db("""
        SELECT p.id, p.content, p.created_at, u.id AS user_id, u.username, u.location,
               u.created_at AS user_created_at
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE p.is_intro = 1
        ORDER BY ((julianday('now') - julianday(u.created_at)) < ?) DESC, p.created_at DESC
        LIMIT ?
    """, (NEWCOMER_DAYS, INTRO_LIST_SIZE)) or []

    intros = []
    for r in rows:
        comments = []
        for c in (query_db(
                "SELECT c.id, c.content, c.created_at, u.username FROM comments c "
                "JOIN users u ON u.id = c.user_id WHERE c.post_id = ? ORDER BY c.created_at ASC",
                (r['id'],)) or []):
            comments.append({'username': c['username'], 'created_at': c['created_at'],
                             'content': moderate_content(c['content'])[0]})
        intros.append({
            'id': r['id'], 'user_id': r['user_id'], 'username': r['username'],
            'location': r['location'], 'created_at': r['created_at'],
            'content': moderate_content(r['content'])[0],
            'is_newcomer': is_newcomer(r['user_created_at']),
            'comments': comments,
        })

    has_intro = query_db("SELECT 1 FROM posts WHERE user_id = ? AND is_intro = 1 LIMIT 1",
                         (user_id,), one=True) is not None
    return render_template('introductions.html.j2', intros=intros, has_intro=has_intro,
                           prompts=INTRO_PROMPTS, newcomer_days=NEWCOMER_DAYS)


# ---------------------------------------------------------------------------
# Design Claim 18 - friendly interactions soon after joining
# An established member sends a personal welcome (a comment on the newcomer's
# introduction / first post). The welcome is recorded so that every newcomer gets
# greeted and the newcomer can see who welcomed them.
# ---------------------------------------------------------------------------
@app.route('/welcome/<int:newcomer_id>', methods=['POST'])
def welcome_newcomer(newcomer_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to welcome someone.', 'danger')
        return redirect(url_for('login'))

    back = _safe_next_url(request.form.get('next')) or url_for('feed')
    message = (request.form.get('message') or '').strip()

    me = query_db("SELECT created_at FROM users WHERE id = ?", (user_id,), one=True)
    newcomer = query_db("SELECT id, username, created_at FROM users WHERE id = ?",
                        (newcomer_id,), one=True)
    if not newcomer or newcomer['id'] == user_id:
        flash('You cannot welcome this user.', 'warning')
        return redirect(back)
    if not me or is_newcomer(me['created_at']):
        flash('Welcomes are sent by established members.', 'info')
        return redirect(back)
    if not is_newcomer(newcomer['created_at']):
        flash(f"{newcomer['username']} is no longer a newcomer.", 'info')
        return redirect(back)
    if not message or len(message) > WELCOME_MAX_LENGTH:
        flash(f'Please write a welcome of 1-{WELCOME_MAX_LENGTH} characters.', 'warning')
        return redirect(back)

    # Keep welcomes friendly: refuse text that the moderation rules would act on
    _, _, reasons = analyze_content(message)
    if reasons:
        flash('Please keep your welcome friendly: ' + '; '.join(reasons) + '.', 'warning')
        return redirect(back)

    target = query_db("SELECT id FROM posts WHERE user_id = ? ORDER BY is_intro DESC, created_at ASC LIMIT 1",
                      (newcomer_id,), one=True)
    if not target:
        flash(f"{newcomer['username']} has not posted anything yet.", 'info')
        return redirect(back)

    db = get_db()
    try:
        cur = db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                         (target['id'], user_id, message))
        db.execute('INSERT INTO welcomes (newcomer_id, welcomer_id, post_id, comment_id) VALUES (?, ?, ?, ?)',
                   (newcomer_id, user_id, target['id'], cur.lastrowid))
        db.commit()
        flash(f"Your welcome was sent to {newcomer['username']}. Thank you!", 'success')
    except sqlite3.IntegrityError:
        db.rollback()
        flash('You have already welcomed this member.', 'info')
    return redirect(back)


if __name__ == '__main__':
    app.run(debug=True, port=8080)
