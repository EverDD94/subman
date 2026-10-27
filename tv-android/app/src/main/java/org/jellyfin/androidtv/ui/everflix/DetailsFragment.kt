package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
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
			Box(Modifier.fillMaxWidth(0.6f)) { HeroDetails(if (current.type == BaseItemKind.SERIES) nextUp ?: current else current) }

			if (current.type != BaseItemKind.PERSON) Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.padding(top = 20.dp)) {
				val label = when {
					canResume -> "Continuar"
					current.type == BaseItemKind.SERIES && nextUp != null -> "Ver T${nextUp?.parentIndexNumber ?: 1} · E${nextUp?.indexNumber ?: 1}"
					else -> "Reproducir"
				}
				PillButton("▶  $label", primary = true, modifier = Modifier.focusRequester(playButton)) {
					play(target.id)
				}
				if (canResume) PillButton("↺  Desde el principio") {
					play(target.id, fromStart = true)
				}
				PillButton(if (played) "✓  Visto" else "Marcar como visto") {
					scope.launch {
						runCatching { mutations.setPlayed(current.id, !played) }.onSuccess { data -> item = current.copy(userData = data) }
					}
				}
				PillButton(if (favorite) "♥  En mi lista" else "♡  Mi lista") {
					scope.launch {
						runCatching { mutations.setFavorite(current.id, !favorite) }.onSuccess { data -> item = current.copy(userData = data) }
					}
				}
			}

			val streams = target.mediaSources?.firstOrNull()?.mediaStreams.orEmpty()
			val audios = streams.filter { it.type == MediaStreamType.AUDIO }.mapNotNull { it.language }.distinct()
			val subs = streams.filter { it.type == MediaStreamType.SUBTITLE }.mapNotNull { it.language }.distinct()
			LaunchedEffect(target.id) {
				if (subLang == null) subLang = subs.firstOrNull { it in SPANISH } ?: subs.firstOrNull() ?: ""
			}
			if (audios.size > 1 || subs.isNotEmpty()) Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.padding(top = 12.dp)) {
				if (audios.size > 1) LangChip("Audio: ${langName(audioLang ?: audios.first())}") {
					audioLang = audios[(audios.indexOf(audioLang ?: audios.first()) + 1) % audios.size]
				}
				if (subs.isNotEmpty()) LangChip("Subtítulos: ${if (subLang.isNullOrEmpty()) "No" else langName(subLang!!)}") {
					val options = subs + ""
					subLang = options[(options.indexOf(subLang ?: "") + 1) % options.size]
				}
			}

			if (seasons.isNotEmpty()) {
				LazyRow(
					horizontalArrangement = Arrangement.spacedBy(8.dp),
					contentPadding = PaddingValues(end = 56.dp),
					modifier = Modifier.padding(top = 28.dp),
				) {
					items(seasons, key = { it.id }) { s ->
						SeasonChip(s.name ?: "", selected = s.id == season) { season = s.id }
					}
				}
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
private fun LangChip(label: String, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Box(
		Modifier
			.clip(RoundedCornerShape(16.dp))
			.background(if (focused) Ink else Color.Black.copy(alpha = 0.35f))
			.border(1.dp, Ink.copy(alpha = 0.25f), RoundedCornerShape(16.dp))
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.padding(horizontal = 14.dp, vertical = 6.dp),
	) {
		Text("$label  ⇄", color = if (focused) Color.Black else Ink, fontSize = 13.sp, fontWeight = FontWeight.W600)
	}
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
private fun PillButton(label: String, primary: Boolean = false, modifier: Modifier = Modifier, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val bg = when {
		focused -> Ink
		primary -> Accent
		else -> Color.White.copy(alpha = 0.14f)
	}
	Box(
		contentAlignment = Alignment.Center,
		modifier = modifier
			.height(44.dp)
			.scale(if (focused) 1.06f else 1f)
			.clip(RoundedCornerShape(22.dp))
			.background(bg)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.padding(horizontal = 22.dp),
	) {
		Text(label, color = if (focused) Color.Black else Ink, fontSize = 15.sp, fontWeight = FontWeight.W700, maxLines = 1)
	}
}

@Composable
private fun SeasonChip(label: String, selected: Boolean, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Column(
		horizontalAlignment = Alignment.CenterHorizontally,
		modifier = Modifier
			.clip(RoundedCornerShape(6.dp))
			.background(if (focused) Ink else Color.Transparent)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.padding(horizontal = 12.dp, vertical = 6.dp),
	) {
		Text(
			label,
			color = when {
				focused -> Color.Black
				selected -> Ink
				else -> Ink.copy(alpha = 0.55f)
			},
			fontSize = 15.sp,
			fontWeight = FontWeight.W700,
		)
		if (selected && !focused) Box(Modifier.padding(top = 4.dp).width(18.dp).height(3.dp).background(Accent))
	}
}

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
