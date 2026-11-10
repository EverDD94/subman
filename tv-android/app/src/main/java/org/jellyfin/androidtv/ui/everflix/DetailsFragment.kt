package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.text.style.TextAlign
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
private val SoftBlue = Color(0xFF9DB4FF)

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
	// Side panel: "season" or "langs", null when closed
	var picker by remember { mutableStateOf<String?>(null) }
	fun play(id: UUID, fromStart: Boolean = false) {
		audioLang?.let { videoQueueManager.setLastPlayedAudioLanguageIsoCode(it) }
		videoQueueManager.setLastPlayedSubtitleLanguageIsoCode(subLang)
		if (fromStart) playbackHelper.retrieveAndPlay(listOf(id), false, 0L, 0, context)
		else playbackHelper.retrieveAndPlay(id, false, context)
	}

	LaunchedEffect(id) {
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
		runCatching { playButton.requestFocus() }.onFailure { runCatching { firstPoster.requestFocus() } }
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

	Box(Modifier.fillMaxSize().background(Bg)) {
		PreviewBackdrop(api, series ?: current)

		Column(
			Modifier
				.fillMaxSize()
				.verticalScroll(rememberScrollState())
				.padding(start = 56.dp, top = 48.dp, bottom = 32.dp),
		) {
			Box(Modifier.fillMaxWidth().padding(end = 56.dp, top = 24.dp), contentAlignment = Alignment.Center) {
				Box(Modifier.fillMaxWidth(0.72f)) { HeroDetails(if (current.type == BaseItemKind.SERIES) nextUp ?: current else current, centered = true) }
			}

			val streams = target.mediaSources?.firstOrNull()?.mediaStreams.orEmpty()
			val audios = streams.filter { it.type == MediaStreamType.AUDIO }.mapNotNull { it.language }.distinct()
			val subs = streams.filter { it.type == MediaStreamType.SUBTITLE }.mapNotNull { it.language }.distinct()
			LaunchedEffect(target.id) {
				if (subLang == null) subLang = subs.firstOrNull { it in SPANISH } ?: subs.firstOrNull() ?: ""
			}
			val langText = listOfNotNull(
				if (audios.size > 1) langName(audioLang ?: audios.first()) else null,
				if (subs.isNotEmpty()) (if (subLang.isNullOrEmpty()) "Sin subtítulos" else "Sub. ${langName(subLang!!)}") else null,
			).joinToString(" · ")
			val seasonName = seasons.firstOrNull { it.id == season }?.name ?: "Temporada"

			// Everflix floating bar (same as Roku): one centred row of glass tiles, the hint below
			if (current.type != BaseItemKind.PERSON) {
				var hint by remember { mutableStateOf("") }
				Row(
					horizontalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterHorizontally),
					verticalAlignment = Alignment.CenterVertically,
					modifier = Modifier.fillMaxWidth().padding(end = 56.dp, top = 28.dp),
				) {
					val label = when {
						canResume -> "Continuar"
						current.type == BaseItemKind.SERIES && nextUp != null -> "Ver T${nextUp?.parentIndexNumber ?: 1} · E${nextUp?.indexNumber ?: 1}"
						else -> "Reproducir"
					}
					GlassTile("▶", label, primary = true, modifier = Modifier.focusRequester(playButton), onFocus = { hint = "" }) {
						play(target.id)
					}
					if (canResume) GlassTile("↺", "Inicio", onFocus = { hint = "Ver desde el principio" }) {
						play(target.id, fromStart = true)
					}
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
					if (langText.isNotEmpty()) GlassTile("Aa", "Idioma", onFocus = { hint = "Audio y subtítulos · $langText" }) {
						picker = "langs"
					}
					if (seasons.size > 1) GlassTile("⌄", seasonShort(seasonName), onFocus = { hint = "${episodes.size} episodios · elige temporada" }) {
						picker = "season"
					} else if (seasons.size == 1) GlassTile("▭", seasonShort(seasonName), onFocus = { hint = "${episodes.size} episodios" }) {}
				}
				Text(
					hint,
					color = Ink.copy(alpha = 0.6f),
					fontSize = 13.sp,
					fontWeight = FontWeight.W600,
					textAlign = TextAlign.Center,
					modifier = Modifier.fillMaxWidth().padding(end = 56.dp, top = 12.dp).height(20.dp),
				)
			}

			if (seasons.isNotEmpty()) {
				LazyRow(
					horizontalArrangement = Arrangement.spacedBy(14.dp),
					contentPadding = PaddingValues(top = 14.dp, end = 56.dp, bottom = 8.dp),
					// Up from any episode goes back to the bar (Play), not to a random tile
					modifier = Modifier.focusProperties { up = playButton },
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

		picker?.let { mode ->
			val t = target
			val st = t.mediaSources?.firstOrNull()?.mediaStreams.orEmpty()
			val audios = st.filter { it.type == MediaStreamType.AUDIO }.mapNotNull { it.language }.distinct()
			val subs = st.filter { it.type == MediaStreamType.SUBTITLE }.mapNotNull { it.language }.distinct()
			val options: List<Pair<String, () -> Unit>> = if (mode == "season") {
				seasons.map { s -> ((if (s.id == season) "✓  " else "") + (s.name ?: "")) to { season = s.id } }
			} else {
				val a = if (audios.size > 1) audios.map { code -> ((if (code == (audioLang ?: audios.first())) "✓  " else "") + "Audio: " + langName(code)) to { audioLang = code } } else emptyList()
				val sb = (subs + "").map { code -> ((if (code == (subLang ?: "")) "✓  " else "") + "Subtítulos: " + (if (code.isEmpty()) "No" else langName(code))) to { subLang = code } }
				a + sb
			}
			SidePanel(if (mode == "season") "Temporadas" else "Idioma", options, onClose = {
				picker = null
				runCatching { playButton.requestFocus() }
			})
		}
	}
}

private fun seasonShort(name: String): String {
	val n = Regex("\\d+").find(name)?.value
	return if (n != null && name.contains("emporada", ignoreCase = true)) "Temp. $n" else name.take(14)
}

/** Glass tile for the floating bar: square glass with icon and label; Play is wide and white. Grows a little with a ring on focus. */
@Composable
private fun GlassTile(
	icon: String,
	label: String,
	primary: Boolean = false,
	selected: Boolean = false,
	modifier: Modifier = Modifier,
	onFocus: () -> Unit = {},
	onClick: () -> Unit,
) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	LaunchedEffect(focused) { if (focused) onFocus() }
	val grow by animateFloatAsState(if (focused) 1.08f else 1f, label = "grow")
	val shape = RoundedCornerShape(16.dp)
	Box(
		modifier
			.scale(grow)
			.then(if (focused) Modifier.border(3.dp, Ink, RoundedCornerShape(19.dp)).padding(4.dp) else Modifier.padding(4.dp))
			.width(if (primary) 120.dp else 62.dp)
			.height(62.dp)
			.clip(shape)
			.background(if (primary) Ink else Color.White.copy(alpha = if (focused) 0.28f else 0.10f))
			.border(1.dp, Color.White.copy(alpha = if (primary) 0f else 0.18f), shape)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		if (primary) Column(Modifier.align(Alignment.CenterStart).padding(start = 12.dp)) {
			Text(icon, color = Color.Black, fontSize = 18.sp, fontWeight = FontWeight.W800)
			Text(label, color = Color.Black, fontSize = 13.sp, fontWeight = FontWeight.W800, maxLines = 1, overflow = TextOverflow.Ellipsis)
		} else Column(Modifier.align(Alignment.Center), horizontalAlignment = Alignment.CenterHorizontally) {
			Text(icon, color = if (selected) SoftBlue else Ink, fontSize = 20.sp, fontWeight = FontWeight.W700)
			Text(label, color = Ink, fontSize = 9.sp, fontWeight = FontWeight.W600, maxLines = 1, overflow = TextOverflow.Ellipsis)
		}
	}
}

/** Right-hand panel listing seasons or languages; Back closes it. */
@Composable
private fun SidePanel(title: String, options: List<Pair<String, () -> Unit>>, onClose: () -> Unit) {
	val first = remember { FocusRequester() }
	BackHandler { onClose() }
	LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
	Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.45f))) {
		Column(
			Modifier
				.align(Alignment.CenterEnd)
				.fillMaxHeight()
				.width(300.dp)
				.background(Color(0xF0141822))
				.padding(28.dp)
				.verticalScroll(rememberScrollState()),
			verticalArrangement = Arrangement.spacedBy(6.dp),
		) {
			Text(title, color = Ink, fontSize = 20.sp, fontWeight = FontWeight.W800, modifier = Modifier.padding(bottom = 12.dp))
			options.forEachIndexed { i, (text, pick) ->
				val interaction = remember { MutableInteractionSource() }
				val focused by interaction.collectIsFocusedAsState()
				Text(
					text,
					color = if (focused) Color.Black else Ink,
					fontSize = 15.sp,
					fontWeight = FontWeight.W700,
					modifier = Modifier
						.fillMaxWidth()
						.then(if (i == 0) Modifier.focusRequester(first) else Modifier)
						.clip(RoundedCornerShape(10.dp))
						.background(if (focused) Ink else Color.Transparent)
						.clickable(interactionSource = interaction, indication = null) { pick(); onClose() }
						.padding(horizontal = 14.dp, vertical = 10.dp),
				)
			}
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
