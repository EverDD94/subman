package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.mutableIntStateOf
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.jellyfin.androidtv.data.repository.ItemMutationRepository
import org.jellyfin.androidtv.data.repository.ItemRepository
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.home.HeroDetails
import org.jellyfin.androidtv.ui.navigation.Destinations
import org.jellyfin.androidtv.ui.navigation.NavigationRepository
import org.jellyfin.androidtv.util.PlaybackHelper
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.imageApi
import org.jellyfin.sdk.api.client.extensions.itemsApi
import org.jellyfin.sdk.api.client.extensions.libraryApi
import org.jellyfin.sdk.api.client.extensions.tvShowsApi
import org.jellyfin.sdk.api.client.extensions.userLibraryApi
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.model.api.BaseItemKind
import org.jellyfin.sdk.model.api.ImageType
import org.jellyfin.sdk.model.api.MediaStreamType
import org.jellyfin.androidtv.ui.playback.VideoQueueManager
import org.koin.compose.koinInject
import timber.log.Timber
import java.util.UUID

private val Ink = Color(0xFFF4F2EE)
private val Accent = Color(0xFF3D63E0)
private val Bg = Color(0xFF0B0D12)

/**
 * Everflix item page: the title big over its backdrop, a few clear buttons, and underneath the
 * episodes of the chosen season (series) or similar titles (movies). Other item types keep the
 * original Jellyfin screen.
 */
class DetailsFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		val id = remember { UUID.fromString(requireArguments().getString("ItemId")) }
		DetailsScreen(id)
	}
}

@Composable
private fun DetailsScreen(id: UUID) {
	val api = koinInject<ApiClient>()
	val navigationRepository = koinInject<NavigationRepository>()
	val playbackHelper = koinInject<PlaybackHelper>()
	val mutations = koinInject<ItemMutationRepository>()
	val context = LocalContext.current
	val scope = rememberCoroutineScope()

	var item by remember { mutableStateOf<BaseItemDto?>(null) }
	var series by remember { mutableStateOf<BaseItemDto?>(null) }
	var nextUp by remember { mutableStateOf<BaseItemDto?>(null) }
	var seasons by remember { mutableStateOf<List<BaseItemDto>>(emptyList()) }
	var season by remember { mutableStateOf<UUID?>(null) }
	var episodes by remember { mutableStateOf<List<BaseItemDto>>(emptyList()) }
	var similar by remember { mutableStateOf<List<BaseItemDto>>(emptyList()) }
	val playButton = remember { FocusRequester() }
	val firstPoster = remember { FocusRequester() }
	val videoQueueManager = koinInject<VideoQueueManager>()
	// Chosen languages; null audio = file default, "" subtitles = off
	var audioLang by remember { mutableStateOf<String?>(null) }
	var subLang by remember { mutableStateOf<String?>(null) }
	fun play(id: UUID, fromStart: Boolean = false) {
		audioLang?.let { videoQueueManager.setLastPlayedAudioLanguageIsoCode(it) }
		videoQueueManager.setLastPlayedSubtitleLanguageIsoCode(subLang)
		if (fromStart) playbackHelper.retrieveAndPlay(listOf(id), false, 0L, 0, context)
		else playbackHelper.retrieveAndPlay(id, false, context)
	}

	// Bumped each time the screen comes back (e.g. from the player) to refresh progress and ask for a rating
	var resumes by remember { mutableIntStateOf(0) }
	val lifecycle = androidx.lifecycle.compose.LocalLifecycleOwner.current.lifecycle
	DisposableEffect(lifecycle) {
		val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_RESUME) resumes++ }
		lifecycle.addObserver(observer)
		onDispose { lifecycle.removeObserver(observer) }
	}
	val videotecario = remember { Videotecario(api.baseUrl, api.accessToken) }
	var rating by remember { mutableStateOf<Int?>(null) }
	var canRate by remember { mutableStateOf(false) }
	val askedRating = remember { mutableSetOf<String>() }
	var panel by remember { mutableStateOf<String?>(null) }

	LaunchedEffect(id, resumes) {
		val firstLoad = item == null
		val loaded = withContext(Dispatchers.IO) {
			runCatching { api.userLibraryApi.getItem(itemId = id).content }.onFailure { Timber.w(it) }.getOrNull()
		} ?: return@LaunchedEffect
		if (loaded.type !in setOf(BaseItemKind.SERIES, BaseItemKind.MOVIE, BaseItemKind.EPISODE, BaseItemKind.BOX_SET, BaseItemKind.PERSON)) {
			navigationRepository.navigate(Destinations.legacyItemDetails(id), replace = true)
			return@LaunchedEffect
		}
		item = loaded
		withContext(Dispatchers.IO) {
			runCatching {
				val seriesId = if (loaded.type == BaseItemKind.EPISODE) loaded.seriesId else loaded.id.takeIf { loaded.type == BaseItemKind.SERIES }
				if (seriesId != null) {
					series = if (seriesId == loaded.id) loaded else api.userLibraryApi.getItem(itemId = seriesId).content
					if (loaded.type == BaseItemKind.SERIES) {
						nextUp = api.tvShowsApi.getNextUp(seriesId = seriesId, enableResumable = true, fields = ItemRepository.itemFields, limit = 1).content.items.firstOrNull()
					}
					seasons = api.tvShowsApi.getSeasons(seriesId = seriesId).content.items
					season = loaded.seasonId ?: nextUp?.seasonId ?: seasons.firstOrNull()?.id
				} else if (loaded.type == BaseItemKind.BOX_SET || loaded.type == BaseItemKind.PERSON) {
					similar = api.itemsApi.getItems(
						parentId = loaded.id.takeIf { loaded.type == BaseItemKind.BOX_SET },
						personIds = if (loaded.type == BaseItemKind.PERSON) setOf(loaded.id) else null,
						recursive = true,
						includeItemTypes = setOf(BaseItemKind.MOVIE, BaseItemKind.SERIES),
						sortBy = setOf(org.jellyfin.sdk.model.api.ItemSortBy.PREMIERE_DATE),
						fields = ItemRepository.itemFields,
					).content.items
				} else {
					similar = api.libraryApi.getSimilarItems(itemId = loaded.id, limit = 20, fields = ItemRepository.itemFields).content.items
				}
			}.onFailure { Timber.w(it, "Failed to load details") }
		}
		// Rating as on the Videotecario web; only the admin can (his taste profile). Episodes rate their series.
		val rateSrc = if (loaded.type == BaseItemKind.EPISODE) series else loaded
		val kind = when (rateSrc?.type) { BaseItemKind.MOVIE -> "movie"; BaseItemKind.SERIES -> "tv"; else -> null }
		val tmdb = rateSrc?.providerIds?.entries?.firstOrNull { it.key.equals("Tmdb", true) }?.value?.toIntOrNull()
		if (kind != null && tmdb != null) {
			runCatching { videotecario.rating(kind, tmdb) }
				.onSuccess { rating = it; canRate = true }
				.onFailure { canRate = false }
			val key = "$kind$tmdb"
			if (!firstLoad && canRate && (rating == null || rating == 0) && askedRating.add(key)) panel = "rate"
		}
		if (firstLoad) runCatching { playButton.requestFocus() }.onFailure { runCatching { firstPoster.requestFocus() } }
	}

	LaunchedEffect(season) {
		val seriesId = series?.id ?: return@LaunchedEffect
		val seasonId = season ?: return@LaunchedEffect
		episodes = withContext(Dispatchers.IO) {
			runCatching {
				api.tvShowsApi.getEpisodes(seriesId = seriesId, seasonId = seasonId, fields = ItemRepository.itemFields).content.items
			}.getOrDefault(emptyList())
		}
	}

	val current = item
	if (current == null) {
		Box(Modifier.fillMaxSize().background(Bg))
		return
	}
	val target = nextUp ?: current
	val canResume = (target.userData?.playbackPositionTicks ?: 0L) > 0L
	val played = current.userData?.played == true
	val favorite = current.userData?.isFavorite == true

	val streams = target.mediaSources?.firstOrNull()?.mediaStreams.orEmpty()
	val audios = streams.filter { it.type == MediaStreamType.AUDIO }.mapNotNull { it.language }.distinct()
	val subs = streams.filter { it.type == MediaStreamType.SUBTITLE }.mapNotNull { it.language }.distinct()
	LaunchedEffect(target.id) {
		if (subLang == null) subLang = subs.firstOrNull { it in SPANISH } ?: subs.firstOrNull() ?: ""
	}
	val rateSrc = if (current.type == BaseItemKind.EPISODE) series else current
	fun saveRating(value: Int?) {
		val src = rateSrc ?: return
		val kind = if (src.type == BaseItemKind.MOVIE) "movie" else "tv"
		val tmdb = src.providerIds?.entries?.firstOrNull { it.key.equals("Tmdb", true) }?.value?.toIntOrNull() ?: return
		rating = value
		scope.launch { videotecario.rate(kind, tmdb, value, src.name, src.productionYear) }
	}
	var hint by remember { mutableStateOf("") }

	Box(Modifier.fillMaxSize().background(Bg)) {
		PreviewBackdrop(api, series ?: current)

		Column(
			horizontalAlignment = Alignment.CenterHorizontally,
			modifier = Modifier
				.fillMaxSize()
				.verticalScroll(rememberScrollState())
				.padding(top = 40.dp, bottom = 32.dp),
		) {
			Box(Modifier.fillMaxWidth(0.7f)) { HeroDetails(if (current.type == BaseItemKind.SERIES) nextUp ?: current else current, centered = true) }

			// Floating bar of glass cards
			if (current.type != BaseItemKind.PERSON) Row(
				horizontalArrangement = Arrangement.spacedBy(12.dp),
				verticalAlignment = Alignment.CenterVertically,
				modifier = Modifier.padding(top = 24.dp),
			) {
				val label = when {
					canResume -> "Continuar"
					current.type == BaseItemKind.SERIES && nextUp != null -> "Ver T${nextUp?.parentIndexNumber ?: 1} · E${nextUp?.indexNumber ?: 1}"
					else -> "Reproducir"
				}
				GlassTile("▶", label, primary = true, modifier = Modifier.focusRequester(playButton), onFocus = { hint = "" }) { play(target.id) }
				if (canResume) GlassTile("↺", "Inicio", onFocus = { hint = "Ver desde el principio" }) { play(target.id, fromStart = true) }
				GlassTile("✓", "Visto", selected = played, onFocus = { hint = if (played) "Visto · pulsa para desmarcar" else "Marcar como visto" }) {
					scope.launch {
						runCatching { mutations.setPlayed(current.id, !played) }.onSuccess { data -> item = current.copy(userData = data) }
					}
				}
				GlassTile(if (favorite) "♥" else "♡", "Mi lista", selected = favorite, onFocus = { hint = if (favorite) "En mi lista · pulsa para quitar" else "Añadir a mi lista" }) {
					scope.launch {
						runCatching { mutations.setFavorite(current.id, !favorite) }.onSuccess { data -> item = current.copy(userData = data) }
					}
				}
				if (canRate) GlassTile("★", RATINGS[rating] ?: "Calificar", selected = RATINGS.containsKey(rating), onFocus = { hint = "Califica para mejorar tus recomendaciones" }) { panel = "rate" }
				if (audios.size > 1 || subs.isNotEmpty()) GlassTile("🌐", "Idioma", onFocus = {
					hint = "Audio y subtítulos · " + listOfNotNull(
						if (audios.size > 1) langName(audioLang ?: audios.first()) else null,
						if (subs.isNotEmpty()) (if (subLang.isNullOrEmpty()) "Sin subtítulos" else "Sub. " + langName(subLang!!)) else null,
					).joinToString(" · ")
				}) { panel = "langs" }
				if (seasons.isNotEmpty()) {
					val name = seasons.firstOrNull { it.id == season }?.name ?: "Temporada"
					GlassTile("⌄", name.replace(Regex("^Temporada ", RegexOption.IGNORE_CASE), "Temp. "), onFocus = { hint = "${episodes.size} episodios · elige temporada" }) {
						if (seasons.size > 1) panel = "season"
					}
				}
			}
			Text(hint, color = Ink.copy(alpha = 0.6f), fontSize = 13.sp, fontWeight = FontWeight.W600, modifier = Modifier.padding(top = 10.dp).height(18.dp))

			Column(Modifier.fillMaxWidth().padding(start = 56.dp)) {
				if (seasons.isNotEmpty()) {
					LazyRow(
						horizontalArrangement = Arrangement.spacedBy(14.dp),
						contentPadding = PaddingValues(top = 14.dp, end = 56.dp, bottom = 8.dp),
					) {
						itemsIndexed(episodes, key = { _, it -> it.id }) { _, ep ->
							EpisodeCard(api, ep) { play(ep.id) }
						}
					}
				} else if (similar.isNotEmpty()) {
					SectionTitle(
						when (current.type) {
							BaseItemKind.BOX_SET -> "EN ESTA COLECCIÓN · ${similar.size}"
							BaseItemKind.PERSON -> "APARECE EN · ${similar.size}"
							else -> "MÁS COMO ESTA"
						}
					)
					LazyRow(
						horizontalArrangement = Arrangement.spacedBy(14.dp),
						contentPadding = PaddingValues(top = 10.dp, end = 56.dp, bottom = 8.dp),
					) {
						itemsIndexed(similar, key = { _, it -> it.id }) { index, other ->
							PosterCard(api, other, if (index == 0) Modifier.focusRequester(firstPoster) else Modifier) { navigationRepository.navigate(Destinations.itemDetails(other.id)) }
						}
					}
				}
			}
		}

		when (panel) {
			"season" -> SidePanel(
				title = series?.name ?: "Temporadas",
				options = seasons.map { PanelOption(it.id.toString(), it.name ?: "", selected = it.id == season) },
				onDismiss = { panel = null },
			) { key -> season = UUID.fromString(key); panel = null }
			"langs" -> SidePanel(
				title = "Audio y subtítulos",
				options = buildList {
					if (audios.size > 1) {
						add(PanelOption("", "AUDIO", header = true))
						audios.forEach { add(PanelOption("a:$it", langName(it), selected = it == (audioLang ?: audios.first()))) }
					}
					if (subs.isNotEmpty()) {
						add(PanelOption("", "SUBTÍTULOS", header = true))
						subs.forEach { add(PanelOption("s:$it", langName(it), selected = it == subLang)) }
						add(PanelOption("s:", "Sin subtítulos", selected = subLang.isNullOrEmpty()))
					}
				},
				onDismiss = { panel = null },
			) { key ->
				if (key.startsWith("a:")) audioLang = key.removePrefix("a:")
				if (key.startsWith("s:")) subLang = key.removePrefix("s:")
			}
			"rate" -> SidePanel(
				title = "¿Qué te pareció?",
				options = listOf(
					PanelOption("2", "Me encanta", selected = rating == 2),
					PanelOption("1", "Me gusta", selected = rating == 1),
					PanelOption("-1", "No me gusta", selected = rating == -1),
					PanelOption("x", "Quitar calificación"),
				),
				onDismiss = { panel = null },
			) { key -> saveRating(key.toIntOrNull()); panel = null }
		}
	}
}

private val RATINGS = mapOf(2 to "Me encanta", 1 to "Me gusta", -1 to "No me gusta")

private data class PanelOption(val key: String, val text: String, val selected: Boolean = false, val header: Boolean = false)

/** Side sheet to pick one option (seasons, languages, rating). Back closes it. */
@Composable
private fun SidePanel(title: String, options: List<PanelOption>, onDismiss: () -> Unit, onPick: (String) -> Unit) {
	val first = remember { FocusRequester() }
	BackHandler(onBack = onDismiss)
	Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.65f)).clickable(indication = null, interactionSource = remember { MutableInteractionSource() }, onClick = onDismiss)) {
		Column(
			Modifier
				.align(Alignment.CenterEnd)
				.fillMaxHeight()
				.width(320.dp)
				.background(Color(0xF512151C))
				.verticalScroll(rememberScrollState())
				.padding(horizontal = 20.dp, vertical = 44.dp),
			verticalArrangement = Arrangement.spacedBy(6.dp),
		) {
			Text(title, color = Ink, fontSize = 20.sp, fontWeight = FontWeight.W800, modifier = Modifier.padding(start = 12.dp, bottom = 14.dp))
			val focusIndex = options.indexOfFirst { it.selected && !it.header }.takeIf { it >= 0 } ?: options.indexOfFirst { !it.header }
			options.forEachIndexed { i, o ->
				if (o.header) {
					Text(o.text, color = Ink.copy(alpha = 0.5f), fontSize = 11.sp, fontWeight = FontWeight.W800, letterSpacing = 1.sp, modifier = Modifier.padding(start = 12.dp, top = 10.dp))
				} else {
					val interaction = remember { MutableInteractionSource() }
					val focused by interaction.collectIsFocusedAsState()
					Row(
						verticalAlignment = Alignment.CenterVertically,
						modifier = Modifier
							.fillMaxWidth()
							.then(if (i == focusIndex) Modifier.focusRequester(first) else Modifier)
							.clip(RoundedCornerShape(10.dp))
							.background(if (focused) Ink else Color.Transparent)
							.clickable(interactionSource = interaction, indication = null) { onPick(o.key) }
							.padding(horizontal = 12.dp, vertical = 10.dp),
					) {
						Text(o.text, color = if (focused) Color.Black else Ink.copy(alpha = if (o.selected) 1f else 0.7f), fontSize = 16.sp, fontWeight = FontWeight.W600, modifier = Modifier.weight(1f))
						if (o.selected) Text("✓", color = if (focused) Color.Black else Color(0xFF9DB4FF), fontSize = 16.sp)
					}
				}
			}
		}
	}
	LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
}

/** Glass card button: icon over a short name; the primary one is wide and white. Grows a little when focused. */
@Composable
private fun GlassTile(icon: String, label: String, primary: Boolean = false, selected: Boolean = false, modifier: Modifier = Modifier, onFocus: () -> Unit = {}, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	LaunchedEffect(focused) { if (focused) onFocus() }
	val scale by animateFloatAsState(if (focused) 1.08f else 1f, animationSpec = tween(180, easing = FastOutSlowInEasing), label = "tile")
	val shape = RoundedCornerShape(14.dp)
	Box(
		modifier = modifier
			.scale(scale)
			.then(if (focused) Modifier.border(2.dp, Color.White, shape) else Modifier.border(1.dp, Color.White.copy(alpha = 0.18f), shape))
			.clip(shape)
			.background(
				when {
					primary -> Ink
					focused -> Color.White.copy(alpha = 0.28f)
					else -> Color.White.copy(alpha = 0.10f)
				}
			)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.height(76.dp)
			.width(if (primary) 150.dp else 76.dp),
	) {
		if (primary) Column(Modifier.padding(start = 14.dp, top = 12.dp)) {
			Text(icon, color = Color.Black, fontSize = 20.sp)
			Text(label, color = Color.Black, fontSize = 15.sp, fontWeight = FontWeight.W800, maxLines = 1, overflow = TextOverflow.Ellipsis)
		} else Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxSize().padding(top = 12.dp)) {
			Text(icon, color = if (selected) Color(0xFF9DB4FF) else Ink, fontSize = 22.sp)
			Text(label, color = Ink, fontSize = 11.sp, fontWeight = FontWeight.W600, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 4.dp, start = 4.dp, end = 4.dp))
		}
	}
}

private val SPANISH = setOf("spa", "es", "esp")

private fun langName(code: String): String = when (code.lowercase()) {
	"spa", "es", "esp" -> "Español"
	"eng", "en" -> "Inglés"
	"por", "pt" -> "Portugués"
	"fre", "fra", "fr" -> "Francés"
	"jpn", "ja" -> "Japonés"
	"ita", "it" -> "Italiano"
	"ger", "deu", "de" -> "Alemán"
	else -> java.util.Locale.forLanguageTag(code).getDisplayLanguage(java.util.Locale("es")).replaceFirstChar { it.uppercase() }.ifBlank { code }
}

/** Small chip that cycles through its options on each press, easy with a TV remote. */
@Composable
private fun SectionTitle(text: String) = Text(
	text = text,
	color = Ink.copy(alpha = 0.8f),
	fontSize = 13.sp,
	fontWeight = FontWeight.W800,
	letterSpacing = 1.sp,
	modifier = Modifier.padding(top = 28.dp),
)

@Composable
private fun EpisodeCard(api: ApiClient, ep: BaseItemDto, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val run = ep.runTimeTicks ?: 0L
	val pos = ep.userData?.playbackPositionTicks ?: 0L
	Column(
		modifier = Modifier
			.width(250.dp)
			.scale(if (focused) 1.06f else 1f)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		Box(
			Modifier
				.fillMaxWidth()
				.aspectRatio(16f / 9f)
				.clip(RoundedCornerShape(8.dp))
				.then(if (focused) Modifier.border(3.dp, Ink, RoundedCornerShape(8.dp)) else Modifier)
				.background(Color(0xFF161A22)),
		) {
			AsyncImage(
				url = api.imageApi.getItemImageUrl(itemId = ep.id, imageType = ImageType.PRIMARY, maxWidth = 500),
				scaleType = ImageView.ScaleType.CENTER_CROP,
				modifier = Modifier.fillMaxSize(),
			)
			if (ep.userData?.played == true) Text(
				"✓",
				color = Ink,
				fontWeight = FontWeight.W800,
				modifier = Modifier.align(Alignment.TopEnd).padding(6.dp).clip(RoundedCornerShape(10.dp)).background(Accent).padding(horizontal = 7.dp),
			)
			if (pos > 0 && run > 0) Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(4.dp).background(Color.White.copy(alpha = 0.25f))) {
				Box(Modifier.fillMaxWidth((pos.toFloat() / run).coerceIn(0f, 1f)).height(4.dp).background(Accent))
			}
		}
		Text(
			"${ep.indexNumber ?: ""}. ${ep.name ?: ""}",
			color = if (focused) Ink else Ink.copy(alpha = 0.8f),
			fontSize = 14.sp,
			fontWeight = FontWeight.W700,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(top = 8.dp),
		)
		if (run > 0) Text("${run / 600_000_000L} min", color = Ink.copy(alpha = 0.5f), fontSize = 12.sp)
	}
}

@Composable
private fun PosterCard(api: ApiClient, item: BaseItemDto, modifier: Modifier = Modifier, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Box(
		modifier
			.width(120.dp)
			.aspectRatio(2f / 3f)
			.scale(if (focused) 1.08f else 1f)
			.clip(RoundedCornerShape(8.dp))
			.then(if (focused) Modifier.border(3.dp, Ink, RoundedCornerShape(8.dp)) else Modifier)
			.background(Color(0xFF161A22))
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		AsyncImage(
			url = api.imageApi.getItemImageUrl(itemId = item.id, imageType = ImageType.PRIMARY, maxWidth = 300),
			scaleType = ImageView.ScaleType.CENTER_CROP,
			modifier = Modifier.fillMaxSize(),
		)
	}
}
